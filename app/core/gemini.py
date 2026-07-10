"""Shared Gemini (Google GenAI) client + typed structured-generation helper.

One place to construct the `google-genai` client from settings and to run
schema-constrained JSON generation. Used first by the knowledge-graph
entity/relationship extractor (spec 5.3); later by the router and specialists.

Free-tier note (see project memory + spec 9): the default model everywhere is
`gemini-2.5-flash-lite`. `gemini-2.5-pro` is `limit: 0` on the free tier, so the
spec's "2.5 Pro for extraction" is deferred until billing is enabled — the model
is a parameter so it can be bumped later without code changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TypeVar

from google import genai
from google.genai import errors, types
from pydantic import BaseModel
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.core.logging import get_logger
from app.core.tracing import observe, record_generation_usage

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ImagePart:
    """One image to inline into a multimodal generation.

    `label` is emitted as a text part immediately *before* the pixels, so the
    model binds the image to the marker the caller will ask it to cite (e.g.
    `[img1]`). Without the interleaved label, several images in one request are
    indistinguishable to the model.
    """

    data: bytes
    mime_type: str
    label: str = ""


def _contents(prompt: str, images: Sequence[ImagePart]) -> str | list[types.Part]:
    """Build the request body: a bare string when text-only, otherwise the
    labelled images followed by the prompt."""
    if not images:
        return prompt
    parts: list[types.Part] = []
    for img in images:
        if img.label:
            parts.append(types.Part.from_text(text=img.label))
        parts.append(types.Part.from_bytes(data=img.data, mime_type=img.mime_type))
    parts.append(types.Part.from_text(text=prompt))
    return parts


@lru_cache
def get_client() -> genai.Client:
    """Return a cached google-genai client built from settings."""
    if not settings.google_api_key:
        raise RuntimeError("GOOGLE_API_KEY is not set; cannot call Gemini.")
    return genai.Client(api_key=settings.google_api_key)


def _is_retryable(exc: BaseException) -> bool:
    """True for transient Gemini failures worth retrying:

    - 429 RESOURCE_EXHAUSTED — per-minute free-tier throttle (clears in ~30s)
    - 5xx (500/503 UNAVAILABLE) — model overloaded / transient server errors
    """
    if isinstance(exc, errors.APIError):
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code == 429 or "RESOURCE_EXHAUSTED" in str(exc):
            return True
        return isinstance(code, int) and 500 <= code < 600
    return False


# @observe wraps @retry so a single generation span covers all retry attempts.
@observe(as_type="generation", name="gemini.generate_structured")
@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=15, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def generate_structured(
    *,
    prompt: str,
    schema: type[T] | type[list[T]],
    model: str | None = None,
    temperature: float = 0.0,
    system_instruction: str | None = None,
) -> T | list[T]:
    """Run a schema-constrained JSON generation and return `response.parsed`.

    `schema` may be a Pydantic model or a `list[Model]`. The parsed value is a
    validated instance (or list) of that type. Retries on free-tier 429s with
    exponential backoff (the per-minute throttle clears in ~30s).
    """
    client = get_client()
    model = model or settings.gemini_router_model
    config = types.GenerateContentConfig(
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=schema,
        system_instruction=system_instruction,
    )
    resp = client.models.generate_content(model=model, contents=prompt, config=config)
    record_generation_usage(resp, model=model)
    if resp.parsed is None:
        raise RuntimeError(
            f"Gemini returned no parseable structured output. Raw: {resp.text[:500]!r}"
        )
    return resp.parsed


@observe(as_type="generation", name="gemini.generate_text")
@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=15, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def generate_text(
    *,
    prompt: str,
    model: str | None = None,
    temperature: float = 0.2,
    system_instruction: str | None = None,
    images: Sequence[ImagePart] = (),
) -> str:
    """Run a text generation and return the response text.

    Used by the specialist agents (spec 7.3), which emit free-form prose with
    inline `[n]` citation markers rather than a fixed JSON shape. Retries on
    free-tier 429s / transient 5xx with exponential backoff, same as
    `generate_structured`.

    `images` inlines figures into the request so the model reasons over the
    pixels, not just the caption; each is introduced by its `label`. The response
    is still plain text — a model that wants to surface an image does so by
    writing that image's marker (see `app/agents/figures.py`).
    """
    client = get_client()
    model = model or settings.gemini_router_model
    config = types.GenerateContentConfig(
        temperature=temperature,
        system_instruction=system_instruction,
    )
    resp = client.models.generate_content(
        model=model, contents=_contents(prompt, images), config=config
    )
    record_generation_usage(resp, model=model)
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gemini returned an empty text response.")
    return text
