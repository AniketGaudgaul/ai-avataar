"""Multi-pass, schema-constrained graph extraction (spec 5.3 steps 2-3).

Three passes on purpose:

1. **Entity pass** — supply the 8 node types as the only allowed types; the model
   returns canonical entities, merging surface variants (AWS Bedrock / Bedrock /
   Amazon Bedrock -> one node).
2. **General relationship pass** — feed the entity list back in; the model connects
   Person/Company/Role/Publication/Achievement entities (WORKED_AT, HELD_ROLE, AT,
   LED, PUBLISHED, WON, PART_OF).
3. **Per-project edge pass** — USED and DEMONSTRATES are pulled out into their own
   pass, run once per Project entity. `gemini-2.5-flash-lite` reliably got these two
   edge types backwards even with explicit direction instructions (it kept attributing
   a project's tech/skills directly to the Person), so instead of asking the model for
   `source_id` at all, we fix it in code to the current project and only ask which
   Technology/Skill entities belong to it. That makes the wrong-direction failure mode
   structurally impossible rather than merely discouraged.

Model: `gemini-2.5-flash-lite` (free-tier constraint). The spec calls for 2.5 Pro
here; bump `model=` once billing is enabled — pass 3 may become unnecessary with a
stronger model that respects direction instructions.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from app.config import settings
from app.core.gemini import generate_structured
from app.core.logging import get_logger
from app.ingestion.graph.schema import (
    RELATION_SCHEMA,
    ExtractedEntity,
    ExtractedRelationship,
    NodeType,
    ProposedGraph,
    RelationType,
)

logger = get_logger(__name__)

# USED/DEMONSTRATES are handled by the dedicated per-project pass (see module
# docstring); the general relationship pass only sees the remaining types.
_PROJECT_SCOPED_TYPES = {RelationType.USED, RelationType.DEMONSTRATES}
_GENERAL_TYPES = {t: eps for t, eps in RELATION_SCHEMA.items() if t not in _PROJECT_SCOPED_TYPES}

# --- Ontology prose injected into the prompts -----------------------------

NODE_TYPE_GUIDE = {
    NodeType.PERSON: "An individual person (the resume owner and any named people).",
    NodeType.COMPANY: "An employer or organization worked at / for (e.g. Yarnit, IIT Patna).",
    NodeType.ROLE: "A job title / position held (e.g. 'Generative AI Engineer').",
    NodeType.PROJECT: "A named piece of work, product, or system.",
    NodeType.SKILL: "A demonstrated capability (e.g. 'Retrieval-Augmented Generation').",
    NodeType.TECHNOLOGY: "A concrete tool, library, framework, model, or platform (e.g. 'Neo4j').",
    NodeType.PUBLICATION: "A paper or published research (e.g. the ECIR 2024 MedSumm paper).",
    NodeType.ACHIEVEMENT: "An award, prize, competition win, or formal recognition.",
}


def _node_type_block() -> str:
    return "\n".join(f"- {t.value}: {desc}" for t, desc in NODE_TYPE_GUIDE.items())


def _relation_schema_block(
    schema: dict[RelationType, tuple[NodeType, NodeType]] | None = None,
) -> str:
    lines = []
    for rel, (src, tgt) in (schema or RELATION_SCHEMA).items():
        lines.append(f"- {rel.value}: ({src.value})->({tgt.value})")
    return "\n".join(lines)


ENTITY_SYSTEM = (
    "You are a meticulous knowledge-graph entity extractor building the entity layer "
    "of a personal career knowledge graph that will power a career Q&A assistant. "
    "You extract only canonical entities of an allowed, fixed set of types, merging "
    "duplicates before you emit them. You never invent facts not present in the text, "
    "and you never turn organizational scaffolding (section headings, category labels) "
    "into entities."
)

RELATION_SYSTEM = (
    "You are a meticulous knowledge-graph relationship extractor, running as the "
    "second pass over a FIXED set of already-extracted entities. You connect them "
    "using only an allowed set of edge types and endpoint-type pairs, always "
    "attributing project-level facts (technologies used, skills demonstrated) to the "
    "Project entity rather than the Person. You never introduce new entities and "
    "never invent relationships not supported by the text."
)


def _entity_prompt(marked_corpus: str) -> str:
    return f"""\
# ROLE
You are a meticulous knowledge-graph entity extractor building the first layer of a
personal career knowledge graph — the graph that will let a Q&A assistant answer
questions like "what companies has he worked at" or "what's the common thread across
his projects" by traversing entities and relationships instead of guessing from prose.
This entity pass runs BEFORE any relationship is extracted, so its output — the fixed
set of canonical entities — is the only vocabulary the next pass will be allowed to
connect. Anything you miss or mis-shape here is unrecoverable later; anything noisy
you include pollutes every downstream query.

# OBJECTIVE
Read the source text and produce a canonical, deduplicated list of entities, each
strictly typed against the 8-type ontology below. Precision matters as much as recall:
a wrong or noisy entity is worse than a missed one, because it will silently show up
in citations and traversals as if it were a real fact about the person's career.

# CONTEXT
The source text is a concatenation of career documents (resume today; a personal
narrative will be added later), each wrapped in a provenance marker block such as
[RESUME]...[/RESUME] or [NARRATIVE]...[/NARRATIVE]. Resumes are terse and list-heavy:
they mix job history, project bullets, a flat "Technical Skills" section grouped under
category headings, and an achievements list. Your job is to recognize which of these
surface forms map to real entities and which are just organizational scaffolding
(section headings, category labels) that should produce no entity at all.

ALLOWED NODE TYPES (use ONLY these; discard anything that doesn't fit one exactly):
{_node_type_block()}

# GUIDELINES

## What counts as a Skill (be strict — this is the type most prone to noise)
- A Skill is a demonstrated CAPABILITY or COMPETENCY exercised on a specific project or
  role — something you'd cite as evidence of expertise (e.g. "Retrieval-Augmented
  Generation", "Multi-Agent Systems", "Prompt Engineering", "LLM Evaluation").
- Do NOT create a Skill from a resume section/category heading — "LLM & AI Systems",
  "Frameworks & Libraries", "Data & Storage", "Backend & Infrastructure", "Languages",
  "LLM Stack" are HEADINGS that group other entities; they are not entities themselves
  and must be skipped entirely.
- Do NOT create a Skill from a bare domain/industry word used as context — "biotech",
  "retail", "marketing" describe a project's domain. Encode that as a `domain` property
  on the relevant Project instead of a standalone Skill node.
- Do NOT create a Skill from a generic noun phrase with no capability content — "URLs",
  "abbreviations", "medical terms", "drug names", "internal documents", "research
  papers" are inputs or artifacts mentioned in passing, not skills.
- Do NOT create a Skill from a raw metric or count standing alone — "1,000+ concurrent
  users", "5,000+ SKUs" are evidence/scale, and belong as a property on the Project they
  describe, not as their own entity.
- The "Technical Skills" resume section DOES list genuine Skill and Technology
  entities under its category headings — extract the listed items themselves (e.g.
  "Prompt Engineering", "LLMOps"), just never the heading label they're grouped under.

## Deduplication (merge before you emit, not after)
- Output canonical entities only. MERGE surface variants into ONE entity and list the
  others in `aliases` — e.g. "AWS Bedrock" / "Bedrock" / "Amazon Bedrock" become one
  Technology with canonical_name "AWS Bedrock" and the rest as aliases.
- If the same award, competition, or event is named more than once across the
  document (e.g. once in the summary, again in an experience bullet, possibly with a
  slightly different full name), merge it into ONE Achievement entity — do not let
  phrasing differences produce two nodes for one real-world thing.

## Field conventions
- `id` is a stable lowercase kebab-case slug derived from the canonical name (e.g.
  "yarnit-innovations", "agentic-rag-presentation-generator").
- `canonical_name` is the clearest, most complete human-readable name.
- `properties` is a list of key/value pairs for attributes explicitly stated in the
  text (e.g. dates, metrics like "domain_misattribution_reduction": "60%", location,
  cgpa, and a Project's `domain` if one is stated). Do NOT fabricate properties that
  aren't in the text.
- `source_spans` are short verbatim snippets (a phrase or clause) the entity was drawn
  from — this is what lets a human reviewer verify you didn't invent it.
- `sources` lists which marker block(s) the entity appeared in: "resume", "narrative",
  "project", "paper", "achievements", or "how_i_built_this".
- Be comprehensive within the real entities, but never duplicate. Prefer specific
  Technology/Skill nodes ("LangGraph") over vague ones ("a framework").

# SOURCE TEXT
{marked_corpus}
"""


def _relationship_prompt(marked_corpus: str, entities: list[ExtractedEntity]) -> str:
    entity_index = [
        {"id": e.id, "type": e.type.value, "canonical_name": e.canonical_name}
        for e in entities
    ]
    return f"""\
# ROLE
You are a meticulous knowledge-graph relationship extractor, running as the second pass
of a multi-pass pipeline. A prior pass already read the same source text and produced
the FIXED list of entities below — your only job now is to connect them with typed
edges. This graph is what will let a downstream Q&A agent answer relational questions
by traversal (e.g. "what companies has he worked at and when") instead of re-reading
prose, so an edge you fail to draw is a query the graph will not be able to answer
later.

# OBJECTIVE
Extract every relationship the source text supports between the given entities, typed
against the fixed relationship schema below, with correct edge direction and endpoint
types in every case.

# CONTEXT
Entities are grouped conceptually as: a Person, the Companies/Roles they held, the
Projects they led, and the Publications/Achievements attributed to them. This pass
covers employment, role, authorship, and award relationships — NOT which technologies
or skills a project used or demonstrated (that is handled separately by a later pass
scoped to each project, so do not attempt USED or DEMONSTRATES edges here even though
you may see Technology/Skill entities in the list below — simply ignore them for this
pass).

ENTITIES (you may ONLY reference these ids; never invent new entities):
{json.dumps(entity_index, indent=2)}

ALLOWED RELATIONSHIP TYPES and their endpoint types (source)->(target):
{_relation_schema_block(_GENERAL_TYPES)}

# GUIDELINES
- `source_id` and `target_id` MUST both be ids from the ENTITIES list above.
- Respect endpoint types EXACTLY as listed — an edge with the wrong endpoint types is
  silently discarded at load time, wasting that piece of extraction entirely.
- Only assert an edge if the text supports it. Set `confidence` in [0,1] (1.0 =
  explicitly stated, lower = reasonably inferred from context).
- `properties` carries edge attributes as key/value pairs where stated — especially
  `start` and `end` dates for WORKED_AT (e.g. "start": "Aug 2024", "end": "May 2026").
- `source_span` is a short verbatim snippet supporting the edge — this is what lets a
  human reviewer verify the edge against the source text.
- `sources` lists which marker block(s) supported it ("resume", "narrative", ...).
- Do not duplicate edges. Prefer precise, well-typed edges over speculative ones.

# SOURCE TEXT
{marked_corpus}
"""


class _ProjectEdgeCandidate(BaseModel):
    """Wire model for the per-project USED/DEMONSTRATES pass.

    The model only supplies which candidate entity applies and how confidently —
    never a source/edge-type, since those are fixed in code (see
    `project_scoped_edges_pass`). This makes the wrong-direction failure mode
    structurally impossible rather than merely discouraged by prompt wording.
    """

    target_id: str = Field(description="id of a candidate Technology or Skill entity.")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source_span: str = ""


def _project_scope_prompt(
    marked_corpus: str, project: ExtractedEntity, candidates: list[ExtractedEntity]
) -> str:
    candidate_index = [
        {"id": c.id, "type": c.type.value, "canonical_name": c.canonical_name}
        for c in candidates
    ]
    return f"""\
# ROLE
You are a meticulous knowledge-graph relationship extractor, running as a focused,
project-scoped pass. A prior pass already identified all entities in the source text,
including the single PROJECT below and every candidate Technology/Skill entity in the
whole document. Your only job is to decide which of those candidates truly belong to
THIS ONE project — not to any other project or to the person in general.

# OBJECTIVE
From the candidate list, select every Technology and Skill entity that the source text
explicitly ties to this specific project, and nothing that belongs to a different
project or role.

# CONTEXT
THIS PROJECT:
{json.dumps(
    {
        "id": project.id,
        "canonical_name": project.canonical_name,
        "aliases": project.aliases,
        "source_spans": project.source_spans,
    },
    indent=2,
)}

The source text below contains this project's own description bullets as well as
unrelated material (other projects, employment history, education). Only select
candidates whose use is stated within THIS project's own bullets/description — a
technology used by a different project the person worked on does NOT belong here even
though the person is the same.

CANDIDATE ENTITIES (Technology and Skill only; select by id from this list ONLY):
{json.dumps(candidate_index, indent=2)}

# GUIDELINES
- Be exhaustive for this project: if its bullets mention five technologies, return all
  five, not just the most prominent one. Under-selecting here loses the single most
  valuable part of the graph — the ability to compare tech/skill overlap across
  projects.
- Do NOT select a candidate whose evidence spans belong to a different project, a
  company, or a role rather than to this project.
- `confidence` reflects how explicitly the text ties the candidate to this project
  (1.0 = named directly in this project's bullets, lower = reasonably implied).
- `source_span` is a short verbatim snippet from THIS project's own description that
  supports including the candidate.
- Return an empty list if nothing in the candidate list genuinely belongs to this
  project.

# SOURCE TEXT
{marked_corpus}
"""


def entity_pass(marked_corpus: str, *, model: str | None = None) -> list[ExtractedEntity]:
    """Pass 1: extract canonical entities constrained to the 8 node types."""
    model = model or settings.gemini_router_model
    logger.info("entity pass starting", extra={"model": model, "chars": len(marked_corpus)})
    entities = generate_structured(
        prompt=_entity_prompt(marked_corpus),
        schema=list[ExtractedEntity],
        model=model,
        temperature=0.0,
        system_instruction=ENTITY_SYSTEM,
    )
    logger.info("entity pass complete", extra={"n_entities": len(entities)})
    return entities


def relationship_pass(
    marked_corpus: str,
    entities: list[ExtractedEntity],
    *,
    model: str | None = None,
) -> list[ExtractedRelationship]:
    """Pass 2: extract employment/role/authorship/award edges (not USED/DEMONSTRATES,
    which are handled by `project_scoped_edges_pass`), then drop invalid ones."""
    model = model or settings.gemini_router_model
    logger.info("relationship pass starting", extra={"model": model, "n_entities": len(entities)})
    edges = generate_structured(
        prompt=_relationship_prompt(marked_corpus, entities),
        schema=list[ExtractedRelationship],
        model=model,
        temperature=0.0,
        system_instruction=RELATION_SYSTEM,
    )
    valid = _validate_edges(edges, entities, schema=_GENERAL_TYPES)
    logger.info(
        "relationship pass complete",
        extra={"n_edges_raw": len(edges), "n_edges_valid": len(valid)},
    )
    return valid


def project_scoped_edges_pass(
    marked_corpus: str,
    project: ExtractedEntity,
    entities: list[ExtractedEntity],
    *,
    model: str | None = None,
) -> list[ExtractedRelationship]:
    """Pass 3: for one Project, ask which Technology/Skill entities belong to it.

    `source_id` is fixed to `project.id` in code and never requested from the model,
    so this pass cannot reproduce the Person->Skill/Technology direction error that
    the combined pass was prone to on this model.
    """
    model = model or settings.gemini_router_model
    candidates = [e for e in entities if e.type in (NodeType.TECHNOLOGY, NodeType.SKILL)]
    if not candidates:
        return []

    picks = generate_structured(
        prompt=_project_scope_prompt(marked_corpus, project, candidates),
        schema=list[_ProjectEdgeCandidate],
        model=model,
        temperature=0.0,
        system_instruction=RELATION_SYSTEM,
    )
    by_id = {c.id: c for c in candidates}
    edges: list[ExtractedRelationship] = []
    for pick in picks:
        target = by_id.get(pick.target_id)
        if target is None:
            logger.warning(
                "project-scoped pick dropped: unknown candidate id",
                extra={"project": project.id, "target_id": pick.target_id},
            )
            continue
        edge_type = (
            RelationType.USED if target.type == NodeType.TECHNOLOGY else RelationType.DEMONSTRATES
        )
        edges.append(
            ExtractedRelationship(
                source_id=project.id,
                type=edge_type,
                target_id=target.id,
                source_span=pick.source_span,
                confidence=pick.confidence,
                sources=project.sources,
            )
        )
    logger.info(
        "project-scoped edges pass complete",
        extra={"project": project.id, "n_candidates": len(candidates), "n_edges": len(edges)},
    )
    return edges


def _validate_edges(
    edges: list[ExtractedRelationship],
    entities: list[ExtractedEntity],
    *,
    schema: dict[RelationType, tuple[NodeType, NodeType]] | None = None,
) -> list[ExtractedRelationship]:
    """Drop edges that reference unknown ids or violate endpoint-type rules."""
    schema = schema or RELATION_SCHEMA
    by_id = {e.id: e for e in entities}
    valid: list[ExtractedRelationship] = []
    for edge in edges:
        src = by_id.get(edge.source_id)
        tgt = by_id.get(edge.target_id)
        if src is None or tgt is None:
            logger.warning("edge dropped: unknown endpoint id", extra={"edge": edge.model_dump()})
            continue
        expected = schema.get(edge.type)
        if expected and (src.type, tgt.type) != expected:
            logger.warning(
                "edge dropped: endpoint types violate schema",
                extra={
                    "type": edge.type.value,
                    "got": f"({src.type.value})->({tgt.type.value})",
                    "expected": f"({expected[0].value})->({expected[1].value})",
                },
            )
            continue
        valid.append(edge)
    return valid


def extract_graph(marked_corpus: str, *, model: str | None = None) -> ProposedGraph:
    """Run entity extraction, general relationships, and per-project edges."""
    entities = entity_pass(marked_corpus, model=model)
    relationships = relationship_pass(marked_corpus, entities, model=model)

    projects = [e for e in entities if e.type == NodeType.PROJECT]
    for project in projects:
        relationships += project_scoped_edges_pass(marked_corpus, project, entities, model=model)

    return ProposedGraph(entities=entities, relationships=relationships)
