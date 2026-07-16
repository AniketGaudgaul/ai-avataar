"""Router node (spec 7.3) — the single LLM call that plans retrieval, not just
classifies.

One `gemini-2.5-flash-lite` structured call (low temperature), given the query
*and the recent conversation*, produces: the `route` (which specialist answers),
the `retrieval_plan` (vector / graph / hybrid / none), a **rewritten
`search_query`** for vector search (resolving follow-ups + expanding, never a
verbatim echo), the graph `entities` to resolve, an optional `project_tag` filter
(validated against the known-project catalog), and an `answer_depth` (overview
vs detail — the abstract-first flow). Out-of-scope queries are forced to
`plan="none"` so they skip retrieval and go straight to the refusal node.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agents.catalog import (
    project_catalog_prompt,
    resolve_single_project,
    valid_project_tags,
)
from app.agents.prompts import ROUTER_SYSTEM
from app.agents.state import AnswerDepth, AvatarState, RetrievalPlan, Route
from app.config import settings
from app.core.gemini import generate_structured
from app.core.logging import get_logger
from app.core.tracing import observe, span_update

logger = get_logger(__name__)


class RouterDecision(BaseModel):
    """Structured router output (spec 7.1, extended to a query plan)."""

    route: Route
    retrieval_plan: RetrievalPlan
    search_query: str = Field(
        default="",
        description="Rewritten, self-contained retrieval query (not a verbatim echo).",
    )
    sub_queries: list[str] = Field(
        default_factory=list,
        description="For comparison/multi-part questions only: 2-4 focused "
        "sub-queries, one per entity or aspect. Empty for a single-topic question.",
    )
    entities: list[str] = Field(
        default_factory=list,
        description="Proper-noun entities to resolve in the graph; pronouns about "
        "the subject resolved to his name.",
    )
    project_tag: str = Field(
        default="",
        description="Exact tag of the single project the query targets, or empty.",
    )
    answer_depth: AnswerDepth = "detail"
    visual_intent: bool = Field(
        default=False,
        description="True only when the user explicitly asks to SEE a diagram, "
        "figure, screenshot, or chart.",
    )
    include_profile: bool = Field(
        default=False,
        description="True for broad overview/summary questions about him as a "
        "whole, and for ALL recruiter-fit questions; false for narrow single-fact, "
        "single-project, or meta questions.",
    )
    oos_kind: Literal["refuse", "greeting", "clarify"] = Field(
        default="refuse",
        description="Only meaningful for out_of_scope: refuse (off-limits/unrelated), "
        "greeting (bare hello), or clarify (unintelligible input).",
    )
    clarification: str = Field(
        default="",
        description="A question to ask back when the query is answerable but "
        "ambiguous about which project/subject it means; else empty.",
    )
    rationale: str = ""


def _history_text(messages: list[dict]) -> str:
    turns = messages[-settings.agent_history_turns :] if messages else []
    if not turns:
        return "(no prior conversation)"
    return "\n".join(f"{m.get('role', 'user').capitalize()}: {m.get('content', '')}" for m in turns)


@observe(name="router", as_type="chain", capture_input=False, capture_output=False)
def router_node(state: AvatarState) -> dict:
    """Classify + plan retrieval for the query, using conversation history."""
    query = state["query"]
    history = _history_text(state.get("messages", []))

    # Inject the known-project vocabulary so project_tag can be a valid filter.
    system_instruction = ROUTER_SYSTEM
    catalog = project_catalog_prompt()
    if catalog:
        system_instruction = f"{ROUTER_SYSTEM}\n\n{catalog}"

    decision = generate_structured(
        prompt=(
            f"Recent conversation:\n{history}\n\n"
            f"Classify and plan retrieval for the latest question:\n{query}"
        ),
        schema=RouterDecision,
        model=settings.agent_router_model,
        temperature=0.0,
        system_instruction=system_instruction,
    )

    plan = decision.retrieval_plan
    # Invariant: out-of-scope never retrieves; everything else must.
    if decision.route == "out_of_scope":
        plan = "none"
    elif plan == "none":
        plan = "hybrid"  # a non-refusal lane always needs some grounding
    # Invariant: a deep-dive/recruiter answer is explanatory — the substance
    # (architecture, pipeline decisions, evidence) lives in the document store,
    # not the sparse graph. A graph-only plan there starves the answer and forces
    # a hallucination, so upgrade it to hybrid regardless of what the model chose.
    if decision.route in ("deep_dive", "recruiter") and plan == "graph":
        logger.info("upgraded graph-only plan to hybrid", extra={"route": decision.route})
        plan = "hybrid"

    # A vector/hybrid plan needs a query; fall back to the raw question if the
    # model returned an empty rewrite.
    search_query = decision.search_query.strip() or query

    # Multi-query retrieval: keep only non-empty, distinct sub-queries, capped at 4.
    # A lone sub-query is pointless (it's just the search_query), so require >= 2.
    sub_queries: list[str] = []
    seen_sq: set[str] = set()
    for sq in decision.sub_queries:
        s = sq.strip()
        if s and s.lower() not in seen_sq:
            seen_sq.add(s.lower())
            sub_queries.append(s)
    sub_queries = sub_queries[:4] if len(sub_queries) >= 2 else []

    # Reject a hallucinated project_tag not in the catalog (keeps the filter safe).
    project_tag = decision.project_tag.strip()
    if project_tag and project_tag not in valid_project_tags():
        logger.info("router dropped unknown project_tag", extra={"project_tag": project_tag})
        project_tag = ""

    # Deterministic fallback: the nano router often names the project in `entities`
    # but forgets to set `project_tag`, so a single-project deep-dive/factual query
    # goes unfiltered and pulls other projects' chunks. When exactly one known
    # project is named, scope to it. Skipped for recruiter/meta/broad lanes where a
    # single-project filter would wrongly narrow a whole-profile answer.
    if not project_tag and decision.route in ("deep_dive", "factual"):
        inferred = resolve_single_project(decision.entities)
        if inferred:
            logger.info("router inferred project_tag from entities", extra={"project_tag": inferred})
            project_tag = inferred

    # A recruiter read is always a whole-profile judgment, so guarantee the card
    # there even if the model forgot the flag; out-of-scope never retrieves.
    include_profile = (
        (decision.include_profile or decision.route == "recruiter")
        and decision.route != "out_of_scope"
    )

    # Terminal direct-reply routing (see AvatarState.decline_reason). An
    # out-of-scope turn maps its kind to a message; an in-scope but ambiguous
    # question the router chose to clarify short-circuits to that question instead
    # of guessing which project/subject was meant.
    clarification = decision.clarification.strip()
    if decision.route == "out_of_scope":
        decline_reason = {"greeting": "greeting", "clarify": "clarify"}.get(decision.oos_kind, "")
        clarification = ""  # gibberish uses the generic template, not a model-written line
    elif clarification:
        decline_reason = "clarify"
    else:
        decline_reason = ""

    router_summary = {
        "route": decision.route,
        "retrieval_plan": plan,
        "search_query": search_query,
        "sub_queries": sub_queries,
        "entities": decision.entities,
        "project_tag": project_tag,
        "answer_depth": decision.answer_depth,
        "visual_intent": decision.visual_intent,
        "include_profile": include_profile,
        "decline_reason": decline_reason,
    }
    logger.info("router", extra=router_summary)
    span_update(input={"query": query}, output=router_summary)
    return {
        "route": decision.route,
        "retrieval_plan": plan,
        "search_query": search_query,
        "sub_queries": sub_queries,
        "router_entities": decision.entities,
        "project_tag": project_tag,
        "answer_depth": decision.answer_depth,
        "visual_intent": decision.visual_intent,
        "include_profile": include_profile,
        "decline_reason": decline_reason,
        "clarification": clarification,
        "retry_count": 0,
    }


def route_after_router(state: AvatarState) -> str:
    """Conditional edge: out-of-scope or a router-issued direct reply
    (greeting / clarify) → the reply node; everything else → retrieve."""
    if state["route"] == "out_of_scope" or state.get("decline_reason"):
        return "refuse"
    return "retrieve"
