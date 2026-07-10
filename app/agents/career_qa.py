"""Career Q&A specialist (spec 7, 3rd row of the rubric) — grounded factual,
explanatory, and synthesis answers. Also serves the "meta" lane (how this was
built) with a source-filtered context (spec 7.1)."""

from __future__ import annotations

from app.agents.generate import run_specialist
from app.agents.prompts import CAREER_QA_SYSTEM, META_NOTE, OVERVIEW_NOTE
from app.agents.state import AvatarState
from app.config import settings


def career_qa_node(state: AvatarState) -> dict:
    system_prompt = CAREER_QA_SYSTEM
    if state.get("route") == "meta":
        system_prompt = f"{CAREER_QA_SYSTEM}\n{META_NOTE}"
    if state.get("answer_depth") == "overview":
        system_prompt = f"{system_prompt}\n{OVERVIEW_NOTE}"
    return run_specialist(
        state,
        system_prompt=system_prompt,
        model=settings.agent_career_model,
        temperature=0.2,
    )
