"""Refusal node (spec 7.1, 8) — a polite, scoped decline.

Reached two ways: directly from the router for out-of-scope queries, or from the
guardrail after a failed regeneration. Deliberately templated (no LLM) so the
refusal is deterministic and can't itself hallucinate — the safe terminal state.
"""

from __future__ import annotations

from app.agents.prompts import REFUSAL_MESSAGE
from app.agents.state import AvatarState
from app.core.logging import get_logger
from app.core.tracing import observe, span_update

logger = get_logger(__name__)


@observe(name="refuse", as_type="chain", capture_input=False, capture_output=False)
def refuse_node(state: AvatarState) -> dict:
    logger.info("refuse", extra={"route": state.get("route"), "reason": "out_of_scope_or_guard"})
    span_update(output={"route": state.get("route"), "refused": True})
    return {
        "draft_answer": REFUSAL_MESSAGE,
        "citations": [],
        # A refusal shows nothing: a figure chosen by the rejected draft must not
        # survive into the response alongside the decline.
        "answer_images": [],
        "guardrail_verdict": {"pass": True, "reasons": ["refusal (terminal, safe)"]},
    }
