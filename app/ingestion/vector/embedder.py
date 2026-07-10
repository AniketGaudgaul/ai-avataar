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

Images (spec 5.7) go through the *same* model and land in the same space, so a
text query can match a diagram with no captioning step. One measured refinement:
an image is embedded **fused with its text sidecar** — caption + heading — as two
parts of a single `embed_content` call, not as bare pixels. On this corpus that
raised caption-query top-1 from 2/4 to 4/4 and pulled the image's cosine to a
text query from 0.379 to 0.639. See `images.py` for the full measurement and why
images are still retrieved in their own modality-filtered pass.

Kept behind a thin module so a fallback embedder (spec 5.8 risk row) can be
swapped in without touching the store.
"""

from __future__ import annotations

from pathlib import Path

from google.genai import types
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.core.assets import mime_for
from app.core.gemini import _is_retryable, get_client
from app.core.logging import get_logger
from app.core.tracing import get_langfuse, observe, tracing_enabled

logger = get_logger(__name__)

DOCUMENT_TASK = "RETRIEVAL_DOCUMENT"
QUERY_TASK = "RETRIEVAL_QUERY"


@observe(as_type="embedding", name="gemini.embed")
@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=15, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _embed(contents, *, task_type: str, model: str, dim: int, label: str) -> list[float]:
    """Embed one input (text, image, or an interleaved Content of both) into a
    single vector; retries on transient 429/5xx (shared with Gemini gen)."""
    client = get_client()
    if tracing_enabled():
        get_langfuse().update_current_generation(
            model=model, metadata={"task_type": task_type, "dim": dim}
        )
    resp = client.models.embed_content(
        model=model,
        contents=contents,
        config=types.EmbedContentConfig(output_dimensionality=dim, task_type=task_type),
    )
    if not resp.embeddings:
        raise RuntimeError(f"Gemini returned no embedding for: {label!r}")
    return list(resp.embeddings[0].values)


def _embed_one(text: str, *, task_type: str, model: str, dim: int) -> list[float]:
    return _embed(text, task_type=task_type, model=model, dim=dim, label=text[:80])


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


def embed_image(
    image_path: str | Path,
    *,
    sidecar: str = "",
    mime_type: str | None = None,
    model: str | None = None,
    dim: int | None = None,
) -> list[float]:
    """Embed one image into the shared text/image space (`RETRIEVAL_DOCUMENT`).

    When `sidecar` is given, the caption and the image are sent as two parts of
    one `Content` so the API returns a single fused vector — measurably better
    than embedding the pixels alone (see module docstring)."""
    path = Path(image_path)
    data = path.read_bytes()
    if len(data) > settings.image_max_bytes:
        raise ValueError(
            f"{path.name} is {len(data)} bytes, over image_max_bytes "
            f"({settings.image_max_bytes}); downscale it before embedding."
        )
    if mime_type is None:
        mime_type = mime_for(path)

    image_part = types.Part.from_bytes(data=data, mime_type=mime_type)
    contents = (
        types.Content(role="user", parts=[types.Part.from_text(text=sidecar), image_part])
        if sidecar
        else image_part
    )
    return _embed(
        contents,
        task_type=DOCUMENT_TASK,
        model=model or settings.gemini_embedding_model,
        dim=dim or settings.embedding_dim,
        label=path.name,
    )
