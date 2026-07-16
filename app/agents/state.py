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
    # For comparison / multi-part questions: 2-4 focused sub-queries (one per
    # entity or aspect), retrieved separately and RRF-merged so each target gets
    # its own strong hits instead of one diffuse blended query. Empty = single query.
    sub_queries: list[str]
    project_tag: str              # project filter ("" = no filter)
    answer_depth: AnswerDepth     # overview (gist + offer) vs detail
    visual_intent: bool           # user explicitly asked to *see* a diagram/figure
    include_profile: bool         # inject the canonical résumé profile card ([1])

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

    # --- Terminal direct-reply routing ---
    # When set, the graph skips the specialist and the reply node emits a templated
    # message keyed on the reason. Set by the router (greeting / clarify) or by
    # retrieve (unknown_project grounding gate). Empty/absent → answer normally.
    #   "greeting"        → GREETING_MESSAGE (a hello with no question)
    #   "clarify"         → the `clarification` text, or CLARIFY_MESSAGE (gibberish)
    #   "unknown_project" → PROJECT_UNKNOWN_MESSAGE (deep-dive on a non-project)
    decline_reason: str
    # A specific question to ask back when a query is answerable but ambiguous
    # about which project/subject it means (paired with decline_reason "clarify").
    clarification: str

    # --- Guardrail loop ---
    guardrail_verdict: dict       # {"pass": bool, "reasons": [...]}
    retry_count: int
