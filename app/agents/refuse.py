"""Refusal node (spec 7.1, 8) — a polite, scoped decline.

Reached two ways: directly from the router for out-of-scope queries, or from the
guardrail after a failed regeneration. Deliberately templated (no LLM) so the
refusal is deterministic and can't itself hallucinate — the safe terminal state.
"""

from __future__ import annotations

from app.agents.prompts import (
    CLARIFY_MESSAGE,
    GREETING_MESSAGE,
    PROJECT_UNKNOWN_MESSAGE,
    REFUSAL_MESSAGE,
)
from app.agents.state import AvatarState
from app.core.logging import get_logger
from app.core.tracing import observe, span_update

logger = get_logger(__name__)


def _reply_message(state: AvatarState) -> str:
    """Pick the templated terminal reply for this turn (see decline_reason)."""
    reason = state.get("decline_reason")
    if reason == "greeting":
        return GREETING_MESSAGE
    if reason == "clarify":
        # A specific clarifying question when the router gave one; else the generic
        # "didn't catch that" prompt (gibberish/empty input).
        return state.get("clarification") or CLARIFY_MESSAGE
    if reason == "unknown_project":
        return PROJECT_UNKNOWN_MESSAGE
    return REFUSAL_MESSAGE  # out-of-scope (salary/personal/unrelated) or guard fallback


@observe(name="refuse", as_type="chain", capture_input=False, capture_output=False)
def refuse_node(state: AvatarState) -> dict:
    # Terminal templated reply — greeting, clarify, unknown-project decline, or the
    # standard out-of-scope/guardrail-fallback refusal — chosen by decline_reason.
    decline_reason = state.get("decline_reason")
    message = _reply_message(state)
    logger.info(
        "refuse",
        extra={"route": state.get("route"), "reason": decline_reason or "out_of_scope_or_guard"},
    )
    span_update(output={"route": state.get("route"), "refused": True, "reason": decline_reason})
    return {
        "draft_answer": message,
        "citations": [],
        # A refusal shows nothing: a figure chosen by the rejected draft must not
        # survive into the response alongside the decline.
        "answer_images": [],
        "guardrail_verdict": {"pass": True, "reasons": ["refusal (terminal, safe)"]},
    }
