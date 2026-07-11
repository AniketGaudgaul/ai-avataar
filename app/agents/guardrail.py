"""Guardrail node (spec 8) — the terminal validation gate.

**Rule-based for now, no LLM call** (per the current build decision). It validates
the draft answer against deterministic checks and returns a verdict; the state
machine loops back for one regeneration on failure, then falls to a safe refusal.
An LLM-based faithfulness check can slot in later behind the same interface.

Checks:
1. Grounding — if sources were retrieved, the answer must carry at least one
   `[n]` citation marker (spec 8: every factual claim must cite). A graceful
   natural-language decline ("that hasn't come up in his work") is exempt.
2. No-source assertions — if nothing was retrieved, the answer must be a
   graceful decline, not an asserted fact (guards against fabrication).
3. Out-of-scope leakage — the answer must not surface banned topics
   (compensation figures, personal-life terms).
4. Recruiter hedge — recruiter-lane answers must frame themselves as a read on
   the evidence, not a guarantee (spec 8), in any natural phrasing.
"""

from __future__ import annotations

import re

from app.agents.state import AvatarState
from app.core.logging import get_logger
from app.core.tracing import observe, span_update

logger = get_logger(__name__)

# Grouped markers ("[2, 5]") count as citations too — requiring a bare "[2]" made
# a properly-cited answer look uncited and burned the one regeneration allowance.
# All-digit brackets only, so a figure marker ("[img1]") is never mistaken for one.
_MARKER_RE = re.compile(r"\[\d+(?:\s*,\s*\d+)*\]")

# Terms that, appearing in an answer, signal out-of-scope leakage (spec 8). Kept
# deliberately narrow to avoid false positives on legitimate career content.
_BANNED_TERMS = (
    "salary",
    "compensation",
    "how much does he make",
    "his wife",
    "his husband",
    "his girlfriend",
    "his boyfriend",
    "marital status",
    "home address",
)

# Phrases that mark a legitimate "I don't have that" decline, which is exempt
# from the citation requirement. The twin now declines in natural language
# ("hasn't come up in his work") rather than naming its sources, so match those
# too — not just the old "not in my sources" phrasing.
_DECLINE_MARKERS = (
    "don't have",
    "do not have",
    "not in my sources",
    "isn't in",
    "is not in",
    "no information",
    "no detail",
    "can't speak to",
    "cannot speak to",
    "outside what i can",
    "outside my lane",
    "not something i can",
    "hasn't come up",
    "haven't come up",
    "hasn't really come up",
    "isn't something",
    "not something i",
    "leave that to",
    "leave things like",
)

# Natural ways a recruiter answer frames itself as a read on the evidence rather
# than a guarantee (replaces the old hard requirement of the word "projection").
_HEDGE_MARKERS = (
    "projection",
    "from what he",
    "based on his",
    "based on what he",
    "his background",
    "his track record",
    "the evidence",
    "what he's built",
    "what he's done",
    "would be a",
    "would likely",
    "appears to",
    "my read",
    "not a guarantee",
    "on paper",
)


def _looks_like_decline(answer: str) -> bool:
    low = answer.lower()
    return any(m in low for m in _DECLINE_MARKERS)


@observe(name="guardrail", as_type="guardrail", capture_input=False, capture_output=False)
def guardrail_node(state: AvatarState) -> dict:
    """Validate the draft answer; return {"pass": bool, "reasons": [...]}."""
    answer = state.get("draft_answer", "") or ""
    route = state.get("route")
    low = answer.lower()

    has_grounding = bool(state.get("retrieved_context") or state.get("graph_facts"))
    has_citation = bool(_MARKER_RE.search(answer))
    is_decline = _looks_like_decline(answer)

    reasons: list[str] = []

    # 1. Grounded claims must cite.
    if has_grounding and not has_citation and not is_decline:
        reasons.append("Answer states facts but includes no [n] citation markers.")

    # 2. No sources retrieved → must be a graceful decline, not an assertion.
    if not has_grounding and not is_decline:
        reasons.append(
            "No sources were retrieved, but the answer asserts facts instead of declining."
        )

    # 3. Out-of-scope leakage.
    leaked = [t for t in _BANNED_TERMS if t in low]
    if leaked:
        reasons.append(f"Answer surfaces out-of-scope topic(s): {', '.join(leaked)}.")

    # 4. Recruiter answers must frame themselves as a read on the evidence, not a
    #    guarantee — accept any natural hedge, not just the word "projection".
    if route == "recruiter" and not any(h in low for h in _HEDGE_MARKERS):
        reasons.append("Recruiter-fit answer doesn't frame itself as a read on the evidence.")

    verdict = {"pass": not reasons, "reasons": reasons}
    logger.info("guardrail", extra={"pass": verdict["pass"], "reasons": reasons, "route": route})
    span_update(
        output={"pass": verdict["pass"], "reasons": reasons, "route": route},
        metadata={"has_grounding": has_grounding, "has_citation": has_citation},
    )
    return {"guardrail_verdict": verdict}


def route_after_guard(state: AvatarState) -> str:
    """Conditional edge (spec 7.1): pass → end; fail & first try → regenerate via
    the same specialist; fail again → safe refusal."""
    verdict = state.get("guardrail_verdict", {"pass": True})
    if verdict.get("pass", True):
        return "end"
    if state.get("retry_count", 0) < 1:
        return _SPECIALIST_FOR_ROUTE.get(state.get("route"), "career_qa")
    return "refuse"


_SPECIALIST_FOR_ROUTE = {
    "factual": "career_qa",
    "meta": "career_qa",
    "deep_dive": "deep_dive",
    "recruiter": "recruiter",
}
