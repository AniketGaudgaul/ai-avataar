"""Entry point that drives the compiled state machine for a single turn.

Converts an API request (query + prior turns) into the initial `AvatarState`,
invokes the graph, and projects the terminal state back to the wire shape:
final answer, route, and the citations actually referenced in the answer.
"""

from __future__ import annotations

from app.agents.context import used_citations
from app.agents.graph import get_agent_graph
from app.agents.state import AvatarState
from app.core.logging import get_logger
from app.core.tracing import get_langfuse, propagate_attributes, tracing_enabled

logger = get_logger(__name__)


async def _invoke_graph(query: str, history: list[dict] | None) -> dict:
    """Drive the compiled graph for one turn and project to the wire shape."""
    initial: AvatarState = {
        "query": query,
        "messages": history or [],
        "retry_count": 0,
    }
    graph = get_agent_graph()
    final: AvatarState = await graph.ainvoke(initial)

    answer = final.get("draft_answer", "")
    all_citations = final.get("citations", [])
    cited = used_citations(answer, all_citations) if all_citations else []
    # Already filtered to the figures the answer references, by the node that knows
    # which figures were shown; the `[imgN]` markers stay in `answer` to mark where
    # the client should render each one.
    images = final.get("answer_images", [])

    logger.info(
        "agent complete",
        extra={
            "route": final.get("route"),
            "retry_count": final.get("retry_count", 0),
            "guardrail_pass": final.get("guardrail_verdict", {}).get("pass"),
            "citations": len(cited),
            "images": len(images),
        },
    )
    return {
        "answer": answer,
        "route": final.get("route"),
        "citations": cited,
        "images": images,
        # Kept for the trace summary; not part of the wire response.
        "_retry_count": final.get("retry_count", 0),
        "_guardrail_pass": final.get("guardrail_verdict", {}).get("pass"),
    }


async def run_agent(
    query: str,
    history: list[dict] | None = None,
    *,
    session_id: str | None = None,
) -> dict:
    """Run one turn through router → retrieve → specialist → guardrail.

    Returns `{"answer", "route", "citations", "images"}`. `citations` is filtered
    to the `[n]` markers that appear in the final answer (refusals return none);
    `images` to the `[imgN]` figure markers the specialist chose to include.

    When Langfuse is configured, the whole turn is wrapped in one root trace
    (input=query, output=answer) so every node/generation/retrieval span nests
    under it; `session_id` groups a multi-turn conversation in the Langfuse UI."""
    if not tracing_enabled():
        return _strip_internal(await _invoke_graph(query, history))

    lf = get_langfuse()
    # propagate_attributes must be entered before the root span so session_id and
    # the trace name attach to it and every child.
    with propagate_attributes(session_id=session_id, trace_name="chat_turn"):
        # The root observation's input/output doubles as the trace input/output
        # in Langfuse v4, so no separate set_current_trace_io call is needed.
        with lf.start_as_current_observation(
            name="chat_turn", as_type="agent", input=query
        ) as root:
            result = await _invoke_graph(query, history)
            root.update(
                output={
                    "answer": result["answer"],
                    "route": result["route"],
                    "citations": len(result["citations"]),
                    "images": len(result["images"]),
                    "retry_count": result["_retry_count"],
                    "guardrail_pass": result["_guardrail_pass"],
                }
            )
            return _strip_internal(result)


def _strip_internal(result: dict) -> dict:
    """Drop the trace-only keys so the wire response stays clean."""
    return {k: v for k, v in result.items() if not k.startswith("_")}
