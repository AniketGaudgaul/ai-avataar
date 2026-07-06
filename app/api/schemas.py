"""Request/response models for the API layer.

These mirror the LangGraph `AvatarState` (spec 7.2) at the boundary so the wire
contract is explicit and validated independently of internal state.
"""

from typing import Literal

from pydantic import BaseModel, Field

Route = Literal["factual", "deep_dive", "recruiter", "meta", "out_of_scope"]


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=2000)
    # Optional prior turns for session-scoped memory (spec 7.3).
    history: list[ChatMessage] = Field(default_factory=list)


class Citation(BaseModel):
    label: str  # human-readable source, e.g. "WGU Copilot — Architecture"
    source_type: str | None = None
    ref: str | None = None  # chunk_id or graph edge id


class ChatResponse(BaseModel):
    answer: str
    route: Route | None = None
    citations: list[Citation] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    env: str
