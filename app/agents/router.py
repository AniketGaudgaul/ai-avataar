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

from pydantic import BaseModel, Field

from app.agents.catalog import project_catalog_prompt, valid_project_tags
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

    # A vector/hybrid plan needs a query; fall back to the raw question if the
    # model returned an empty rewrite.
    search_query = decision.search_query.strip() or query

    # Reject a hallucinated project_tag not in the catalog (keeps the filter safe).
    project_tag = decision.project_tag.strip()
    if project_tag and project_tag not in valid_project_tags():
        logger.info("router dropped unknown project_tag", extra={"project_tag": project_tag})
        project_tag = ""

    router_summary = {
        "route": decision.route,
        "retrieval_plan": plan,
        "search_query": search_query,
        "entities": decision.entities,
        "project_tag": project_tag,
        "answer_depth": decision.answer_depth,
        "visual_intent": decision.visual_intent,
    }
    logger.info("router", extra=router_summary)
    span_update(input={"query": query}, output=router_summary)
    return {
        "route": decision.route,
        "retrieval_plan": plan,
        "search_query": search_query,
        "router_entities": decision.entities,
        "project_tag": project_tag,
        "answer_depth": decision.answer_depth,
        "visual_intent": decision.visual_intent,
        "retry_count": 0,
    }


def route_after_router(state: AvatarState) -> str:
    """Conditional edge: out-of-scope → refuse; everything else → retrieve."""
    return "refuse" if state["route"] == "out_of_scope" else "retrieve"
