"""LangGraph shared state (spec 7.2).

`AvatarState` is the single dict threaded through every node of the state
machine. Nodes return *partial* dicts; LangGraph merges them into this shape.
The API boundary (`app/api/schemas.py`) mirrors the externally-visible slice of
this state so the wire contract is validated independently of internals.
"""

from __future__ import annotations

from typing import Literal, TypedDict

from app.agents.figures import ChosenFigure
from app.retrieval.schema import GraphFact, RetrievedContext, RetrievedImage

Route = Literal["factual", "deep_dive", "recruiter", "meta", "out_of_scope"]
RetrievalPlan = Literal["vector", "graph", "hybrid", "none"]
# Answer depth (spec: abstract-first for general project questions).
#   overview → brief gist + offer to go deeper on a specific aspect
#   detail   → full, section-specific answer
AnswerDepth = Literal["overview", "detail"]


class Citation(TypedDict):
    """A source label carried alongside the answer (spec 8: every factual claim
    must cite). `ref` is a chunk/parent id or a comma-joined list of graph
    source docs."""

    label: str
    source_type: str
    ref: str


class AvatarState(TypedDict, total=False):
    """State machine memory (spec 7.2). `total=False` so nodes can populate it
    incrementally — the router fills route/plan, retrieve fills context, etc."""

    # --- Inputs ---
    messages: list[dict]          # prior turns: [{"role": "user"|"assistant", "content": str}]
    query: str                    # latest user turn

    # --- Router output (it plans retrieval, not just classifies) ---
    route: Route
    retrieval_plan: RetrievalPlan
    router_entities: list[str]    # candidate graph entity names to resolve
    search_query: str             # rewritten/expanded query for vector search
    project_tag: str              # project filter ("" = no filter)
    answer_depth: AnswerDepth     # overview (gist + offer) vs detail
    visual_intent: bool           # user explicitly asked to *see* a diagram/figure

    # --- Retrieve output ---
    retrieved_context: list[RetrievedContext]
    graph_facts: list[GraphFact]
    citations: list[Citation]
    context_block: str            # numbered, citation-labelled prompt block
    # Figures shown to the specialist as images, numbered [img1..n] positionally.
    retrieved_images: list[RetrievedImage]

    # --- Specialist output ---
    draft_answer: str
    # The subset of `retrieved_images` the answer actually references, each with
    # the marker it is referenced by — the model chooses, retrieval only offers
    # (see app/agents/figures.py).
    answer_images: list[ChosenFigure]

    # --- Guardrail loop ---
    guardrail_verdict: dict       # {"pass": bool, "reasons": [...]}
    retry_count: int
