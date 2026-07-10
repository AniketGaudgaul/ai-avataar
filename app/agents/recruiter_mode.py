"""Recruiter-Mode specialist (spec 7) — concise, structured fit assessments,
explicitly labelled as a projection from available evidence, not a claim of fact
(spec 8). The projection disclaimer is enforced downstream by the guardrail."""

from __future__ import annotations

from app.agents.generate import run_specialist
from app.agents.prompts import RECRUITER_SYSTEM
from app.agents.state import AvatarState
from app.config import settings


def recruiter_node(state: AvatarState) -> dict:
    return run_specialist(
        state,
        system_prompt=RECRUITER_SYSTEM,
        model=settings.agent_recruiter_model,
        temperature=0.2,
    )
