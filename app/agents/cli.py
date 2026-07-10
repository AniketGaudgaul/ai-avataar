"""Manual driver for the agent state machine.

    python -m app.agents.cli "what companies has he worked at?"
    python -m app.agents.cli "walk me through the WGU Copilot architecture"
    python -m app.agents.cli "what is his salary expectation?"      # → refusal
    python -m app.agents.cli --trace "..."   # also print route/plan/guardrail

Runs one turn end-to-end (router → retrieve → specialist → guardrail) against the
live Qdrant + Neo4j backends. Forces UTF-8 stdout so heading breadcrumbs (`▸`)
don't crash a cp1252 Windows console.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.agents.graph import get_agent_graph
from app.agents.state import AvatarState
from app.core.logging import setup_logging
from app.core.tracing import get_langfuse, tracing_enabled


async def _run(query: str, trace: bool) -> None:
    graph = get_agent_graph()
    initial: AvatarState = {"query": query, "messages": [], "retry_count": 0}
    if tracing_enabled():
        with get_langfuse().start_as_current_observation(
            name="cli_turn", as_type="agent", input=query
        ) as root:
            final: AvatarState = await graph.ainvoke(initial)
            root.update(output=final.get("draft_answer", ""))
    else:
        final = await graph.ainvoke(initial)

    if trace:
        print("── trace ──────────────────────────────────────────")
        print(f"route          : {final.get('route')}")
        print(f"retrieval_plan : {final.get('retrieval_plan')}")
        print(f"search_query   : {final.get('search_query')}")
        print(f"answer_depth   : {final.get('answer_depth')}")
        print(f"project_tag    : {final.get('project_tag') or '(none)'}")
        print(f"entities       : {final.get('router_entities')}")
        print(f"visual_intent  : {final.get('visual_intent', False)}")
        print(f"contexts       : {len(final.get('retrieved_context', []))}")
        print(f"graph_facts    : {len(final.get('graph_facts', []))}")
        print(f"retry_count    : {final.get('retry_count', 0)}")
        print(f"guardrail      : {final.get('guardrail_verdict')}")
        shown = final.get("retrieved_images", [])
        print(f"figures shown  : {len(shown)}")
        for i, img in enumerate(shown, 1):
            print(f"   [img{i}] {img.citation_label}")
            print(f"          {img.image_uri}")
        print("───────────────────────────────────────────────────\n")

    print(final.get("draft_answer", "(no answer)"))

    citations = final.get("citations", [])
    if citations:
        print("\nSources:")
        for c in citations:
            print(f"  - {c['label']} ({c['source_type']})")

    chosen = final.get("answer_images", [])
    if chosen:
        print("\nFigures included in the answer:")
        for f in chosen:
            print(f"  - {f.marker} {f.image.citation_label}")
            print(f"      {f.image.image_uri}")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Drive the AI Avatar agent graph.")
    parser.add_argument("query", help="The user question.")
    parser.add_argument("--trace", action="store_true", help="Print routing/guardrail internals.")
    args = parser.parse_args()

    setup_logging()
    get_langfuse()  # initialise tracing (no-op without keys)
    try:
        asyncio.run(_run(args.query, args.trace))
    finally:
        from app.core.tracing import flush

        flush()  # short-lived process: push queued spans before exit


if __name__ == "__main__":
    main()
