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

from app.agents.context import assemble_context
from app.agents.figures import loadable
from app.agents.profile import PROFILE_CARD
from app.agents.state import AvatarState
from app.config import settings
from app.core.logging import get_logger
from app.core.tracing import observe, span_update
from app.retrieval.graph import facts_for_entities
from app.retrieval.schema import RetrievedImage
from app.retrieval.vector import images_for_retrieved, retrieve_images
from app.retrieval.vector import retrieve as vector_retrieve

logger = get_logger(__name__)

# The meta lane ("how was this built") answers only from the how_i_built_this
# material (spec 7.1: meta folds into Career Q&A with a source filter).
_META_SOURCE_TYPE = "how_i_built_this"


@observe(name="retrieve", as_type="retriever", capture_input=False, capture_output=False)
def retrieve_node(state: AvatarState) -> dict:
    """Run the retrieval plan; fill retrieved_context, graph_facts, citations."""
    plan = state["retrieval_plan"]
    # Use the router's rewritten query for vector search (falls back to raw).
    search_query = state.get("search_query") or state["query"]
    route = state["route"]
    project_tag = state.get("project_tag") or None

    contexts = []
    graph_facts = []
    images: list[RetrievedImage] = []

    if plan in ("vector", "hybrid"):
        source_type = _META_SOURCE_TYPE if route == "meta" else None
        contexts = vector_retrieve(
            search_query,
            limit=settings.agent_max_contexts,
            source_type=source_type,
            project_tag=project_tag,
        )
        # A project_tag that matches no chunks must not cause a false refusal:
        # the project may exist in the graph but have no (or differently-tagged)
        # documents — e.g. the ECIR paper is a Publication with an empty tag, so a
        # query the router scoped to `medsumm-research` filtered to zero. Fall back
        # to unfiltered retrieval, and un-scope the figure pass with it.
        if not contexts and project_tag:
            logger.info(
                "project_tag filter matched no chunks; retrying unfiltered",
                extra={"project_tag": project_tag},
            )
            project_tag = None
            contexts = vector_retrieve(
                search_query,
                limit=settings.agent_max_contexts,
                source_type=source_type,
                project_tag=None,
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

    logger.info(
        "retrieve",
        extra={
            "plan": plan,
            "contexts": len(contexts),
            "graph_facts": len(graph_facts),
            "citations": len(citations),
            "images": len(images),
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
    """Conditional edge after retrieve: dispatch to the specialist for the lane.
    Meta folds into Career Q&A (spec 7.1)."""
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
