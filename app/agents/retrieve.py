"""Retrieve node (spec 7.3) — the shared retrieval step, not duplicated inside
each specialist.

Executes the router's `retrieval_plan`:
- vector / hybrid → hybrid dense+BM25+RRF over the document store (small-to-big
  parent expansion), source-filtered for the meta lane.
- graph / hybrid → resolve the router's entity names to citable graph facts,
  seeding the subject person when the query named no entities.
- figures → the diagrams belonging to the sections the text pass returned, plus,
  when the router saw explicit visual intent, a modality-filtered image search.

The retrieved contexts + graph facts are assembled once into a numbered,
citation-labelled block that every specialist shares. Figures ride along
separately: they are shown to the specialist as images, which decides whether any
of them belongs in the answer (see `app/agents/figures.py`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from functools import lru_cache

from app.agents.catalog import get_project_catalog, resolve_single_project
from app.agents.context import assemble_context
from app.agents.figures import loadable
from app.agents.profile import PROFILE_CARD
from app.agents.state import AvatarState
from app.config import settings
from app.core.logging import get_logger
from app.core.tracing import observe, span_update
from app.retrieval.graph import facts_for_entities
from app.retrieval.schema import GraphFact, RetrievedContext, RetrievedImage
from app.retrieval.vector import images_for_retrieved, retrieve_images
from app.retrieval.vector import retrieve as vector_retrieve

logger = get_logger(__name__)

# The meta lane ("how was this built") answers only from the how_i_built_this
# material (spec 7.1: meta folds into Career Q&A with a source filter).
_META_SOURCE_TYPE = "how_i_built_this"

# Tokens too generic to prove a named subject was actually retrieved — a deep-dive
# entity like "WGU Copilot" must match on a distinctive token ("wgu"/"copilot"),
# not on "project"/"system" appearing in some other project's chunk.
_SUBJECT_STOPWORDS = frozenset({
    "project", "projects", "system", "systems", "app", "application", "the", "and",
    "for", "his", "her", "with", "into", "using", "based", "built", "work", "tool",
    "platform", "pipeline", "model", "assistant", "generator", "research",
})
_SUBJECT_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _subject_tokens(text: str) -> list[str]:
    """Distinctive lowercased tokens of a subject/project name (drops filler)."""
    return [
        t for t in _SUBJECT_TOKEN_RE.findall(text.lower())
        if len(t) >= 3 and t not in _SUBJECT_STOPWORDS
    ]


@lru_cache
def _known_project_terms() -> frozenset[str]:
    """Distinctive tokens of every known project's name + tag (the catalog is the
    authority on which projects he actually worked on)."""
    terms: set[str] = set()
    for p in get_project_catalog():
        terms.update(_subject_tokens(f"{p.get('name', '')} {p.get('id', '')}"))
    return frozenset(terms)


def _subject_unsupported(
    state: AvatarState,
    contexts: list[RetrievedContext],
    graph_facts: list[GraphFact],
) -> bool:
    """True when a deep-dive names a project he never worked on.

    Guards against confidently fabricating an architecture for a non-existent
    project (e.g. "walk me through the WGU Copilot he built"): retrieval always
    returns *some* top-k, so the specialist would otherwise write about whatever
    off-topic chunks came back. Scoped to deep_dive (the fabrication-prone lane)
    and deliberately conservative — it fires only when EVERY named subject is
    neither (a) a known catalog project nor (b) mentioned in real evidence, so any
    genuine project question proceeds.

    Real evidence excludes the "How I Built This" meta doc, which name-drops other
    projects as example queries ("overlap between the hackathon project and WGU
    Copilot") — counting those would mask a bogus subject.
    """
    if state.get("route") != "deep_dive":
        return False
    person = settings.avatar_person_name.strip().lower()
    subjects = [
        e for e in state.get("router_entities", [])
        if e.strip() and e.strip().lower() != person
    ]
    if not subjects:
        return False  # no specific subject to verify → nothing to gate on

    known = _known_project_terms()
    real_evidence = " ".join(
        [c.text for c in contexts if c.source_type != _META_SOURCE_TYPE]
        + [c.heading_path for c in contexts if c.source_type != _META_SOURCE_TYPE]
        + [c.citation_label for c in contexts if c.source_type != _META_SOURCE_TYPE]
        + [f.as_sentence() for f in graph_facts]
    ).lower()

    for subject in subjects:
        tokens = _subject_tokens(subject) or [subject.strip().lower()]
        supported = any(t in known for t in tokens) or any(t in real_evidence for t in tokens)
        if supported:
            return False  # at least one named subject is a real project → proceed
    return True


def _retrieve_one(
    query: str,
    *,
    source_type: str | None,
    project_tag: str | None,
) -> tuple[list[RetrievedContext], str | None]:
    """One vector query with the project_tag-miss fallback.

    Returns `(contexts, effective_tag)` — `effective_tag` is None when the filter
    matched nothing and we fell back to unfiltered, so the caller can un-scope the
    figure pass to match. A project_tag that matches no chunks must not starve the
    answer: the project may exist in the graph but have no (or differently-tagged)
    documents — e.g. the ECIR paper is a Publication with an empty tag, so a query
    scoped to `medsumm-research` filtered to zero."""
    contexts = vector_retrieve(
        query, limit=settings.agent_max_contexts, source_type=source_type, project_tag=project_tag
    )
    if not contexts and project_tag:
        logger.info(
            "project_tag filter matched no chunks; retrying unfiltered",
            extra={"project_tag": project_tag, "query_chars": len(query)},
        )
        contexts = vector_retrieve(
            query, limit=settings.agent_max_contexts, source_type=source_type, project_tag=None
        )
        return contexts, None
    return contexts, project_tag


def _multi_retrieve(
    sub_queries: list[str],
    *,
    source_type: str | None,
    shared_tag: str | None,
) -> list[RetrievedContext]:
    """Retrieve each sub-query separately, then RRF-merge into one ranked list.

    Each sub-query auto-scopes to its own project when it names exactly one (so a
    comparison across A and B fetches A's chunks for A's query and B's for B's,
    instead of one blended query returning a diffuse mix); otherwise it inherits
    the shared tag. Fusing by rank (Reciprocal Rank Fusion) needs no score
    normalization across the independent queries."""
    K = 60  # RRF damping; standard choice
    fused: dict[str, float] = defaultdict(float)
    best: dict[str, RetrievedContext] = {}
    for q in sub_queries:
        tag = resolve_single_project([q]) or shared_tag
        ctxs, _ = _retrieve_one(q, source_type=source_type, project_tag=tag)
        for rank, ctx in enumerate(ctxs):
            fused[ctx.parent_section_id] += 1.0 / (K + rank)
            best.setdefault(ctx.parent_section_id, ctx)
    ranked = sorted(best.values(), key=lambda c: fused[c.parent_section_id], reverse=True)
    return ranked[: settings.agent_max_contexts]


@observe(name="retrieve", as_type="retriever", capture_input=False, capture_output=False)
def retrieve_node(state: AvatarState) -> dict:
    """Run the retrieval plan; fill retrieved_context, graph_facts, citations."""
    plan = state["retrieval_plan"]
    # Use the router's rewritten query for vector search (falls back to raw).
    search_query = state.get("search_query") or state["query"]
    sub_queries = state.get("sub_queries") or []
    route = state["route"]
    project_tag = state.get("project_tag") or None

    contexts = []
    graph_facts = []
    images: list[RetrievedImage] = []

    if plan in ("vector", "hybrid"):
        source_type = _META_SOURCE_TYPE if route == "meta" else None
        if len(sub_queries) >= 2:
            # Comparison / multi-part: retrieve each sub-query and RRF-merge.
            contexts = _multi_retrieve(
                sub_queries, source_type=source_type, shared_tag=project_tag
            )
        else:
            contexts, project_tag = _retrieve_one(
                search_query, source_type=source_type, project_tag=project_tag
            )
        images = _figures_for(
            contexts,
            search_query,
            visual_intent=bool(state.get("visual_intent")),
            source_type=source_type,
            project_tag=project_tag,
        )

    if plan in ("graph", "hybrid"):
        names = [n for n in state.get("router_entities", []) if n.strip()]
        if not names:
            # Relational query that named nothing explicit → it's about the subject.
            names = [settings.avatar_person_name]
        graph_facts = facts_for_entities(_dedupe(names))

    profile_card = PROFILE_CARD if state.get("include_profile") else None
    context_block, citations = assemble_context(
        contexts, graph_facts, profile_card=profile_card
    )

    # Grounding gate: a deep-dive about a subject absent from all evidence gets a
    # confident decline rather than a fabricated answer (see _subject_unsupported).
    decline_reason = ""
    if _subject_unsupported(state, contexts, graph_facts):
        decline_reason = "unknown_project"
        logger.info(
            "grounding gate: named subject unsupported by evidence; declining",
            extra={"entities": state.get("router_entities"), "route": state.get("route")},
        )

    logger.info(
        "retrieve",
        extra={
            "plan": plan,
            "contexts": len(contexts),
            "graph_facts": len(graph_facts),
            "citations": len(citations),
            "images": len(images),
            "decline_reason": decline_reason,
        },
    )
    span_update(
        input={"search_query": search_query, "plan": plan, "project_tag": project_tag},
        output={
            "contexts": len(contexts),
            "graph_facts": len(graph_facts),
            "sources": citations,
            "figures": [i.citation_label for i in images],
        },
    )
    return {
        "retrieved_context": contexts,
        "graph_facts": graph_facts,
        "citations": citations,
        "context_block": context_block,
        "retrieved_images": images,
        "decline_reason": decline_reason,
    }


def _figures_for(
    contexts: list,
    search_query: str,
    *,
    visual_intent: bool,
    source_type: str | None,
    project_tag: str | None,
) -> list[RetrievedImage]:
    """Pick the figures to show the specialist, capped at `agent_max_images`.

    Section anchoring is the default and needs no threshold: a figure inherits the
    relevance of the prose section it illustrates. When the user explicitly asked
    to *see* something, a modality-filtered similarity pass runs too and its hits
    lead — those were ranked against the actual query, whereas anchored figures
    were only ranked against their section, so for "show me the architecture
    diagram" the similarity pass is the one that knows which diagram was meant.

    The two lists' scores are not comparable (the modality gap — see
    `retrieval/vector.py`), so they are concatenated by provenance rather than
    merged by score, and de-duplicated on `chunk_id`.
    """
    anchored = images_for_retrieved(contexts, limit=settings.agent_max_images)
    if not visual_intent:
        return loadable(anchored)[: settings.agent_max_images]

    searched = retrieve_images(search_query, source_type=source_type, project_tag=project_tag)
    seen: set[str] = set()
    ordered: list[RetrievedImage] = []
    for img in [*searched, *anchored]:
        if img.chunk_id in seen:
            continue
        seen.add(img.chunk_id)
        ordered.append(img)
    return loadable(ordered)[: settings.agent_max_images]


def route_to_specialist(state: AvatarState) -> str:
    """Conditional edge after retrieve: dispatch to the specialist for the lane,
    unless the grounding gate flagged an unsupported subject → refuse (a confident
    decline, not a fabricated answer). Meta folds into Career Q&A (spec 7.1)."""
    if state.get("decline_reason"):
        return "refuse"
    return _SPECIALIST_FOR_ROUTE.get(state["route"], "career_qa")


_SPECIALIST_FOR_ROUTE = {
    "factual": "career_qa",
    "meta": "career_qa",
    "deep_dive": "deep_dive",
    "recruiter": "recruiter",
}


def _dedupe(names: list[str]) -> list[str]:
    """Case-insensitive de-dupe, preserving first-seen order."""
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = n.strip().lower()
        if key and key not in seen:
            seen.add(key)
            out.append(n.strip())
    return out
