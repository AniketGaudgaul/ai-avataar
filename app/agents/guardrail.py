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

from app.agents.prompts import persona_present, persona_text
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


# Short generic words that don't signal a claim beyond the brief; excluded so the
# coverage ratio measures *substantive* overlap, not filler.
_STOPWORDS = frozenset({
    "and", "the", "for", "his", "her", "him", "she", "they", "that", "this",
    "with", "from", "have", "has", "into", "here", "there", "then", "than",
    "about", "also", "just", "some", "what", "when", "where", "which", "would",
    "could", "should", "been", "being", "does", "done", "over", "under", "more",
    "very", "aniket",  # the subject's own name says nothing about grounding
})
_WORD_RE = re.compile(r"[a-z][a-z0-9'+.-]{2,}")


def _content_tokens(text: str) -> set[str]:
    """Substantive lowercased words in `text`, with [n] markers and filler removed."""
    stripped = _MARKER_RE.sub(" ", text.lower())
    return {w for w in _WORD_RE.findall(stripped) if w not in _STOPWORDS}


def _persona_covered(answer: str) -> bool:
    """True when the answer is essentially a restatement of the status brief.

    The brief (persona.md) is always-on grounding that carries no [n] markers by
    design, so an answer built from it (location / availability / contact / target
    roles) must not be forced to cite — even when retrieval also returned facts.
    A genuine RAG answer introduces many project/tech terms absent from the brief,
    so its coverage ratio stays well below the threshold and it still needs a
    citation. This is the exemption check #1's comment always intended, keyed on
    *content* rather than on whether retrieval happened to fire.
    """
    ans = _content_tokens(answer)
    persona = _content_tokens(persona_text())
    if not ans or not persona:
        return False
    return len(ans & persona) / len(ans) >= 0.7


@observe(name="guardrail", as_type="guardrail", capture_input=False, capture_output=False)
def guardrail_node(state: AvatarState) -> dict:
    """Validate the draft answer; return {"pass": bool, "reasons": [...]}."""
    answer = state.get("draft_answer", "") or ""
    route = state.get("route")
    low = answer.lower()

    has_rag = bool(state.get("retrieved_context") or state.get("graph_facts"))
    has_citation = bool(_MARKER_RE.search(answer))
    is_decline = _looks_like_decline(answer)
    # The always-on status brief is grounding too, but its facts carry no [n]
    # marker. `has_persona` lets an answer stand when nothing was retrieved;
    # `persona_covered` additionally exempts a brief-only answer from the citation
    # requirement even when retrieval *did* fire (e.g. "Where is he based?" pulls
    # graph facts yet is answered from the brief), so a correct persona answer is
    # no longer rejected and refused.
    has_persona = persona_present()
    persona_covered = has_persona and _persona_covered(answer)

    reasons: list[str] = []

    # 1. Retrieved claims must cite (answers grounded in the status brief are exempt).
    if has_rag and not has_citation and not is_decline and not persona_covered:
        reasons.append("Answer states facts but includes no [n] citation markers.")

    # 2. Nothing to answer from (no retrieval, no status brief) → must decline.
    if not has_rag and not has_persona and not is_decline:
        reasons.append(
            "No sources were retrieved, but the answer asserts facts instead of declining."
        )

    # 3. Out-of-scope leakage. Skipped on the meta lane: a "what guardrails does
    #    this have?" answer legitimately *names* compensation/personal as topics the
    #    system refuses, which is a description, not a disclosure about him.
    leaked = [t for t in _BANNED_TERMS if t in low] if route != "meta" else []
    if leaked:
        reasons.append(f"Answer surfaces out-of-scope topic(s): {', '.join(leaked)}.")

    # 4. Recruiter answers must frame themselves as a read on the evidence, not a
    #    guarantee — accept any natural hedge, not just the word "projection".
    #    A status-brief answer that happens to land in the recruiter lane (e.g.
    #    "is he available?") is a plain fact from the brief, not a fit-judgement, so
    #    a persona-covered answer is exempt from the hedge requirement.
    if route == "recruiter" and not persona_covered and not any(h in low for h in _HEDGE_MARKERS):
        reasons.append("Recruiter-fit answer doesn't frame itself as a read on the evidence.")

    verdict = {"pass": not reasons, "reasons": reasons}
    logger.info("guardrail", extra={"pass": verdict["pass"], "reasons": reasons, "route": route})
    span_update(
        output={"pass": verdict["pass"], "reasons": reasons, "route": route},
        metadata={
            "has_rag": has_rag,
            "has_persona": has_persona,
            "persona_covered": persona_covered,
            "has_citation": has_citation,
        },
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
