"""Dense embeddings via Gemini Embedding 2 (spec 5.8).

One unified, natively-multimodal space; we truncate to 1536 dims via Matryoshka
(`output_dimensionality`) — near-identical quality at half the storage. The API
returns already-L2-normalized vectors at 1536, so cosine search needs no
re-normalization.

Two verified refinements over the spec's assumptions:
- `task_type` IS accepted for `gemini-embedding-2`, so we do proper **asymmetric
  retrieval** — `RETRIEVAL_DOCUMENT` for chunks, `RETRIEVAL_QUERY` for queries —
  rather than the prompt-prefix workaround. This is a real recall lever.
- Embedding is **one input per call** (the API aggregates a multi-input list into
  a single vector), so we embed sequentially. Batch API is a later optimization.

Kept behind a thin module so a fallback embedder (spec 5.8 risk row) can be
swapped in without touching the store.
"""

from __future__ import annotations

from google.genai import types
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.core.gemini import _is_retryable, get_client
from app.core.logging import get_logger

logger = get_logger(__name__)

DOCUMENT_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"


@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=15, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _embed_one(text: str, *, task_type: str, model: str, dim: int) -> list[float]:
    """Embed a single text; retries on transient 429/5xx (shared with Gemini gen)."""
    client = get_client()
    resp = client.models.embed_content(
        model=model,
        contents=text,
        config=types.EmbedContentConfig(output_dimensionality=dim, task_type=task_type),
    )
    if not resp.embeddings:
        raise RuntimeError(f"Gemini returned no embedding for: {text[:80]!r}")
    return list(resp.embeddings[0].values)


def embed_query(text: str, *, model: str | None = None, dim: int | None = None) -> list[float]:
    """Embed a search query (asymmetric: RETRIEVAL_QUERY task)."""
    return _embed_one(
        text,
        task_type=QUERY_TASK,
        model=model or settings.gemini_embedding_model,
        dim=dim or settings.embedding_dim,
    )


def embed_document(text: str, *, model: str | None = None, dim: int | None = None) -> list[float]:
    """Embed one document chunk (asymmetric: RETRIEVAL_DOCUMENT task)."""
    return _embed_one(
        text,
        task_type=DOCUMENT_TASK,
        model=model or settings.gemini_embedding_model,
        dim=dim or settings.embedding_dim,
    )


def embed_documents(
    texts: list[str], *, model: str | None = None, dim: int | None = None
) -> list[list[float]]:
    """Embed many chunks, one API call each (see module note on batching)."""
    model = model or settings.gemini_embedding_model
    dim = dim or settings.embedding_dim
    out: list[list[float]] = []
    for i, text in enumerate(texts):
        out.append(_embed_one(text, task_type=DOCUMENT_TASK, model=model, dim=dim))
        if (i + 1) % 10 == 0 or i + 1 == len(texts):
            logger.info("embedding progress", extra={"done": i + 1, "total": len(texts)})
    return out
