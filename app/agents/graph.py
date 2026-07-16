"""LangGraph state-machine assembly (spec 7.1).

Wires the nodes into the graph:

    START → router ─┬─(out_of_scope)→ refuse → END
                    └─(else)→ retrieve → {career_qa | deep_dive | recruiter} → guard
    guard ─┬─(pass)→ END
           ├─(fail, first try)→ back to the same specialist (regenerate)
           └─(fail again)→ refuse → END

Two design choices from the spec: retrieval is a *shared* node (not duplicated
inside each specialist), and the guardrail loops back *once* before falling to a
safe refusal rather than hard-failing.

`get_agent_graph()` returns the compiled graph as a process-wide singleton.
"""

from __future__ import annotations

from functools import lru_cache

from langgraph.graph import END, START, StateGraph

from app.agents.career_qa import career_qa_node
from app.agents.deep_dive import deep_dive_node
from app.agents.guardrail import guardrail_node, route_after_guard
from app.agents.recruiter_mode import recruiter_node
from app.agents.refuse import refuse_node
from app.agents.retrieve import retrieve_node, route_to_specialist
from app.agents.router import route_after_router, router_node
from app.agents.state import AvatarState


def build_graph() -> StateGraph:
    """Construct (uncompiled) the router → retrieve → specialist → guard graph."""
    g = StateGraph(AvatarState)

    g.add_node("router", router_node)
    g.add_node("retrieve", retrieve_node)
    g.add_node("career_qa", career_qa_node)
    g.add_node("deep_dive", deep_dive_node)
    g.add_node("recruiter", recruiter_node)
    g.add_node("guardrail", guardrail_node)
    g.add_node("refuse", refuse_node)

    g.add_edge(START, "router")

    # Router: out-of-scope → refuse; everything else → retrieve.
    g.add_conditional_edges(
        "router", route_after_router, {"retrieve": "retrieve", "refuse": "refuse"}
    )

    # Retrieve → dispatch to the specialist for the lane, or short-circuit to a
    # confident decline when the grounding gate flags an unsupported subject.
    g.add_conditional_edges(
        "retrieve",
        route_to_specialist,
        {
            "career_qa": "career_qa",
            "deep_dive": "deep_dive",
            "recruiter": "recruiter",
            "refuse": "refuse",
        },
    )

    # Every specialist flows into the guardrail.
    for specialist in ("career_qa", "deep_dive", "recruiter"):
        g.add_edge(specialist, "guardrail")

    # Guardrail: pass → END; fail (first) → regenerate via specialist; fail again → refuse.
    g.add_conditional_edges(
        "guardrail",
        route_after_guard,
        {
            "end": END,
            "career_qa": "career_qa",
            "deep_dive": "deep_dive",
            "recruiter": "recruiter",
            "refuse": "refuse",
        },
    )

    g.add_edge("refuse", END)
    return g


@lru_cache
def get_agent_graph():
    """Compile the state machine once per process (thread-safe to reuse)."""
    return build_graph().compile()
