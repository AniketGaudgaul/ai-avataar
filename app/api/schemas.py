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
    # Optional client-supplied conversation id; groups a multi-turn conversation
    # into one Langfuse session for tracing.
    session_id: str | None = Field(default=None, max_length=128)


class Citation(BaseModel):
    label: str  # human-readable source, e.g. "WGU Copilot — Architecture"
    source_type: str | None = None
    ref: str | None = None  # chunk_id or graph edge id


class AnswerImage(BaseModel):
    """A figure the answer chose to show.

    The `marker` appears inline in `answer` at the spot the figure belongs, so a
    client renders by replacing the marker with the image at `url`. Bytes are
    served by reference rather than base64-inlined — a diagram is often ~1 MB, and
    a client that doesn't render figures should not pay for them."""

    marker: str  # "[img1]" — its position in `answer`
    chunk_id: str
    url: str  # GET this for the bytes
    caption: str
    label: str  # citation label, e.g. "Presentation Generator — Architecture"
    source_type: str | None = None


class ChatResponse(BaseModel):
    answer: str
    route: Route | None = None
    citations: list[Citation] = Field(default_factory=list)
    images: list[AnswerImage] = Field(default_factory=list)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    version: str
    env: str
