"""Langfuse tracing (spec 9 / Phase D observability).

A thin wrapper over the Langfuse **v4** SDK (OpenTelemetry-based). Tracing is
fully optional: with no `langfuse_public_key` / `langfuse_secret_key` configured
the client is built with `tracing_enabled=False` and every `@observe` /
`start_as_current_observation` becomes a cheap no-op — the app behaves identically
whether or not Langfuse is wired up.

Design notes
------------
- **No LangChain callback handler.** Langfuse's `CallbackHandler` hard-requires
  the full `langchain` package (this project deliberately uses only
  `langchain-core` + `langgraph`). Instead we instrument with the OTel-native
  `@observe` decorator: a child span created inside a *sync* LangGraph node run
  under `await graph.ainvoke(...)` nests correctly under the request's root span
  (LangGraph copies the context into its executor — verified empirically), so the
  decorator approach yields a complete trace tree with no extra dependency.

- **Explicit client construction.** `pydantic-settings` reads `.env` into
  `settings` but does NOT export those values to `os.environ`, so Langfuse's own
  `get_client()` (invoked by `@observe`) would otherwise find no credentials. We
  build the singleton from `settings` at startup and also mirror the keys into
  the environment so every resolution path lands on the same project.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from langfuse import Langfuse, observe, propagate_attributes

from app import __version__
from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

__all__ = [
    "get_langfuse",
    "tracing_enabled",
    "observe",
    "propagate_attributes",
    "span_update",
    "record_generation_usage",
    "flush",
    "shutdown",
]


def tracing_enabled() -> bool:
    """True when both Langfuse keys are configured."""
    return bool(settings.langfuse_public_key and settings.langfuse_secret_key)


@lru_cache
def get_langfuse() -> Langfuse:
    """Build (once) the process-wide Langfuse client from settings.

    Returns a disabled no-op client when keys are absent, so callers never need
    to branch on whether tracing is configured."""
    enabled = tracing_enabled()
    if enabled:
        # Mirror into env so Langfuse's own get_client() (used by @observe)
        # resolves this same project without re-reading .env itself.
        os.environ.setdefault("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key)
        os.environ.setdefault("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key)
        os.environ.setdefault("LANGFUSE_BASE_URL", settings.langfuse_host)

    client = Langfuse(
        public_key=settings.langfuse_public_key or None,
        secret_key=settings.langfuse_secret_key or None,
        base_url=settings.langfuse_host or None,
        environment=settings.app_env,
        release=__version__,
        tracing_enabled=enabled,
    )
    logger.info(
        "langfuse initialised",
        extra={"enabled": enabled, "host": settings.langfuse_host if enabled else None},
    )
    return client


def span_update(
    *,
    input: Any = None,
    output: Any = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Attach concise input/output/metadata to the current span.

    Used by graph nodes to record a compact summary (route, counts, verdict)
    rather than the whole `AvatarState`, which would bloat the trace. No-op when
    tracing is disabled."""
    if not tracing_enabled():
        return
    get_langfuse().update_current_span(input=input, output=output, metadata=metadata)


def record_generation_usage(resp: Any, *, model: str) -> None:
    """Record model + token usage on the current generation observation.

    Reads google-genai's `usage_metadata` and maps it to Langfuse's
    `usage_details` (input/output/total, plus cached/reasoning when present) so
    Langfuse can compute cost. No-op when tracing is disabled."""
    if not tracing_enabled():
        return
    usage_details: dict[str, int] = {}
    um = getattr(resp, "usage_metadata", None)
    if um is not None:
        candidates = {
            "input": getattr(um, "prompt_token_count", None),
            "output": getattr(um, "candidates_token_count", None),
            "total": getattr(um, "total_token_count", None),
            "cached": getattr(um, "cached_content_token_count", None),
            "reasoning": getattr(um, "thoughts_token_count", None),
        }
        usage_details = {k: v for k, v in candidates.items() if isinstance(v, int)}
    get_langfuse().update_current_generation(
        model=model, usage_details=usage_details or None
    )


def flush() -> None:
    """Flush queued events (call at the end of a short-lived CLI run)."""
    if tracing_enabled():
        get_langfuse().flush()


def shutdown() -> None:
    """Flush and cleanly shut the client down (FastAPI lifespan shutdown)."""
    if tracing_enabled():
        get_langfuse().shutdown()
