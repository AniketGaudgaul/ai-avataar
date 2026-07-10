"""Shared specialist runner (spec 7.3).

The three specialists differ only in system prompt, model, and verbosity — the
mechanics (assemble prompt from shared context + history, generate, handle the
guardrail regeneration loop) are identical, so they live here once. Each
specialist node is a thin wrapper that supplies its prompt and calls
`run_specialist`.

The generation is multimodal: any figures retrieval anchored to the answer's
sections are attached as images and listed in a FIGURES block, so the specialist
reasons over the diagram itself. It may then surface one to the user by writing
that figure's `[imgN]` marker, which this node validates against the figures
actually shown (see `app/agents/figures.py`).

Regeneration: when a failing `guardrail_verdict` is already in state, this is the
second pass — the failure reasons are appended as feedback and `retry_count` is
bumped, so the guardrail's conditional edge can fall to a safe refusal after one
retry (spec 7.1).
"""

from __future__ import annotations

from app.agents.figures import (
    figure_block,
    load_image_parts,
    strip_unknown_markers,
    used_images,
)
from app.agents.state import AvatarState
from app.config import settings
from app.core.gemini import generate_text
from app.core.logging import get_logger
from app.core.tracing import observe, span_update

logger = get_logger(__name__)


def _history_block(messages: list[dict]) -> str:
    """Render the last few turns for follow-up context (session-scoped only)."""
    turns = messages[-settings.agent_history_turns :] if messages else []
    if not turns:
        return ""
    lines = [f"{m.get('role', 'user').capitalize()}: {m.get('content', '')}" for m in turns]
    return "Recent conversation (for context on follow-ups):\n" + "\n".join(lines) + "\n\n"


@observe(name="specialist", as_type="chain", capture_input=False, capture_output=False)
def run_specialist(
    state: AvatarState,
    *,
    system_prompt: str,
    model: str,
    temperature: float = 0.2,
) -> dict:
    """Generate a grounded answer for the given specialist, handling regen."""
    context_block = state.get("context_block", "")
    query = state["query"]

    verdict = state.get("guardrail_verdict")
    retry_count = state.get("retry_count", 0)
    feedback = ""
    if verdict and not verdict.get("pass", True):
        reasons = "; ".join(verdict.get("reasons", []))
        feedback = (
            "\n\nA previous draft was REJECTED by the guardrail for: "
            f"{reasons}\nProduce a corrected answer that fixes these issues."
        )
        retry_count += 1

    if not context_block.strip():
        context_block = "(No sources were retrieved for this query.)"

    # Figures are attached as image parts *and* described in the prompt; the two
    # must agree, so both number the same `images` list positionally.
    images = state.get("retrieved_images", [])
    image_parts = load_image_parts(images)
    figures = figure_block(images)

    sections = [
        _history_block(state.get("messages", [])).rstrip(),
        f"CONTEXT (numbered sources — cite by their [n] markers):\n{context_block}",
        figures,
        f"QUESTION: {query}{feedback}",
    ]
    prompt = "\n\n".join(s for s in sections if s)

    answer = generate_text(
        prompt=prompt,
        model=model,
        temperature=temperature,
        system_instruction=system_prompt,
        images=image_parts,
    )
    # A marker the model invented would leave a dangling reference in the API
    # response, so drop it before anything downstream reads the answer.
    answer = strip_unknown_markers(answer, len(images))
    chosen = used_images(answer, images)

    logger.info(
        "specialist answer",
        extra={
            "route": state.get("route"),
            "retry_count": retry_count,
            "chars": len(answer),
            "figures_shown": len(image_parts),
            "figures_used": len(chosen),
        },
    )
    span_update(
        input={"route": state.get("route"), "query": query, "model": model},
        output={
            "chars": len(answer),
            "retry_count": retry_count,
            "regenerated": bool(feedback),
            "figures_shown": len(image_parts),
            "figures_used": [i.citation_label for i in chosen],
        },
    )
    return {"draft_answer": answer, "retry_count": retry_count, "answer_images": chosen}
