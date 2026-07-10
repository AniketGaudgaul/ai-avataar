"""Project Deep-Dive specialist (spec 7) — in-depth architecture / technical
walkthroughs. The spec assigns Gemini 2.5 Pro here for quality, but the free tier
forces flash-lite for now (project memory); `agent_deep_dive_model` bumps it back
to Pro when billing is enabled."""

from __future__ import annotations

from app.agents.generate import run_specialist
from app.agents.prompts import DEEP_DIVE_SYSTEM, OVERVIEW_NOTE
from app.agents.state import AvatarState
from app.config import settings


def deep_dive_node(state: AvatarState) -> dict:
    system_prompt = DEEP_DIVE_SYSTEM
    # A general "tell me about project X" gets a gist + an offer to go deeper;
    # a specific-aspect ask (answer_depth="detail") gets the full walkthrough.
    if state.get("answer_depth") == "overview":
        system_prompt = f"{DEEP_DIVE_SYSTEM}\n{OVERVIEW_NOTE}"
    return run_specialist(
        state,
        system_prompt=system_prompt,
        model=settings.agent_deep_dive_model,
        temperature=0.3,
    )
