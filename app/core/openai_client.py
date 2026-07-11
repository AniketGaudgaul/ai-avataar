"""OpenAI generation backend — the alternate implementation behind
`app.core.gemini.generate_structured` / `generate_text`.

Used while Gemini's *generation* billing is unresolved (embeddings stay on
Gemini — see `settings.llm_provider`). The two public functions here mirror the
Gemini helpers' signatures exactly so the dispatcher in `gemini.py` can swap
providers with no change to any caller (router, specialists, extractor).

Three things the GPT-5.x models make us do differently from Gemini:

- **Reasoning models, not temperature.** `gpt-5.4-mini/nano` take
  `reasoning.effort` (none|low|medium|high|xhigh); the `temperature` our callers
  pass is meaningless here and is dropped. Router/extraction run at "none"
  (deterministic classification, no reasoning tokens); specialists at "low".
- **Responses API.** Structured output uses `responses.parse(text_format=Model)`
  → `response.output_parsed`; free-form text uses `responses.create` →
  `output_text`. `text_format` needs an object-rooted schema, so a `list[T]`
  request is wrapped in a one-field model and unwrapped on the way out.
- **Prompt caching is automatic** (prefixes ≥1024 tokens) — our large static
  system prompt already sits at the front of every call. We additionally pass a
  stable `prompt_cache_key` derived from that system prompt so calls sharing a
  prompt (all router calls, all deep-dive calls, …) route to the same cache.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from functools import lru_cache
from hashlib import sha256
from typing import Any, TypeVar, get_args, get_origin

from openai import APIStatusError, APITimeoutError, OpenAI, RateLimitError
from pydantic import BaseModel, create_model
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.core.gemini import ImagePart
from app.core.logging import get_logger
from app.core.tracing import observe, record_openai_usage

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


@lru_cache
def get_openai_client() -> OpenAI:
    """Return a cached OpenAI client built from settings."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not set; cannot call OpenAI.")
    return OpenAI(api_key=settings.openai_api_key)


def _is_retryable(exc: BaseException) -> bool:
    """True for transient OpenAI failures worth retrying: 429 rate limits and
    5xx server errors (mirrors the Gemini retry policy)."""
    if isinstance(exc, (RateLimitError, APITimeoutError)):
        return True
    if isinstance(exc, APIStatusError):
        code = exc.status_code
        return code == 429 or (isinstance(code, int) and 500 <= code < 600)
    return False


def _cache_key(system_instruction: str | None) -> str:
    """A stable prompt_cache_key so calls sharing a system prompt (all router
    calls, all deep-dive calls, …) route to the same cache. Distinct system
    prompts get distinct keys automatically."""
    digest = sha256((system_instruction or "").encode("utf-8")).hexdigest()[:16]
    return f"ai-avatar-{digest}"


@lru_cache
def _list_wrapper(item_type: type[BaseModel]) -> type[BaseModel]:
    """Object-rooted schema for a `list[Item]` request (Responses API structured
    output rejects a bare top-level array)."""
    return create_model(f"{item_type.__name__}List", items=(list[item_type], ...))


def _resolve_schema(schema: Any) -> tuple[bool, type[BaseModel]]:
    """Return (is_list, format_model). A `list[Item]` schema is wrapped; a plain
    model passes through."""
    if get_origin(schema) is list:
        (item_type,) = get_args(schema)
        return True, _list_wrapper(item_type)
    return False, schema


def _reasoning(effort: str | None) -> dict[str, Any]:
    """Build the `reasoning` kwarg, omitting it entirely for effort "none" (let
    the model default) so we never trip a model that rejects an explicit none."""
    if effort and effort != "none":
        return {"reasoning": {"effort": effort}}
    return {}


@observe(as_type="generation", name="openai.generate_structured")
@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=5, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def openai_generate_structured(
    *,
    prompt: str,
    schema: type[T] | type[list[T]],
    model: str | None = None,
    system_instruction: str | None = None,
    reasoning_effort: str | None = None,
) -> T | list[T]:
    """Schema-constrained generation via the Responses API (`responses.parse`).

    Mirrors `app.core.gemini.generate_structured`. `schema` may be a Pydantic
    model or `list[Model]`; the parsed value is a validated instance (or list).
    """
    client = get_openai_client()
    model = model or settings.openai_default_model
    is_list, fmt = _resolve_schema(schema)
    effort = reasoning_effort or settings.openai_reasoning_effort_structured

    resp = client.responses.parse(
        model=model,
        input=prompt,
        instructions=system_instruction,
        text_format=fmt,
        prompt_cache_key=_cache_key(system_instruction),
        **_reasoning(effort),
    )
    record_openai_usage(resp, model=model)
    parsed = resp.output_parsed
    if parsed is None:
        raise RuntimeError(
            f"OpenAI returned no parseable structured output. Raw: {resp.output_text[:500]!r}"
        )
    return parsed.items if is_list else parsed  # type: ignore[attr-defined,return-value]


def _build_input(prompt: str, images: Sequence[ImagePart]) -> str | list[dict[str, Any]]:
    """Build the Responses `input`: a bare string when text-only, otherwise one
    user message whose content interleaves each labelled image (as a base64 data
    URI) before the trailing prompt — same ordering as the Gemini path."""
    if not images:
        return prompt
    content: list[dict[str, Any]] = []
    for img in images:
        if img.label:
            content.append({"type": "input_text", "text": img.label})
        b64 = base64.b64encode(img.data).decode("ascii")
        content.append(
            {"type": "input_image", "image_url": f"data:{img.mime_type};base64,{b64}"}
        )
    content.append({"type": "input_text", "text": prompt})
    return [{"role": "user", "content": content}]


@observe(as_type="generation", name="openai.generate_text")
@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=5, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def openai_generate_text(
    *,
    prompt: str,
    model: str | None = None,
    system_instruction: str | None = None,
    images: Sequence[ImagePart] = (),
    reasoning_effort: str | None = None,
) -> str:
    """Free-form text generation via the Responses API (`responses.create`).

    Mirrors `app.core.gemini.generate_text`, including inlined `images` so the
    model reasons over the pixels; the response is plain text.
    """
    client = get_openai_client()
    model = model or settings.openai_default_model
    effort = reasoning_effort or settings.openai_reasoning_effort_text

    resp = client.responses.create(
        model=model,
        input=_build_input(prompt, images),
        instructions=system_instruction,
        prompt_cache_key=_cache_key(system_instruction),
        **_reasoning(effort),
    )
    record_openai_usage(resp, model=model)
    text = (resp.output_text or "").strip()
    if not text:
        raise RuntimeError("OpenAI returned an empty text response.")
    return text
