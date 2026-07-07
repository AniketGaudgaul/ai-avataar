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

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


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
    if resp.parsed is None:
        raise RuntimeError(
            f"Gemini returned no parseable structured output. Raw: {resp.text[:500]!r}"
        )
    return resp.parsed
