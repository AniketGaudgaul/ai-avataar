"""LlamaParse v2 document parsing (spec 5.4 step 0 — get clean structured markdown).

The ECIR paper is a dense, two-column academic PDF with figures, tables, and
equations — the case where naive text extractors interleave columns into garbage
and destroy the heading structure the chunker depends on. So we use LlamaParse's
premium **agentic_plus** tier (multimodal, layout-aware) to reconstruct reading
order and emit markdown with real heading levels.

Images and tables matter downstream: figures/tables are saved as images (for
Tier-3 multimodal embedding via Gemini Embedding 2) and tables are emitted both
inline in the markdown and as structured items — so we never have to re-parse
(and re-spend LlamaParse credits) when the multimodal path is built.

Uses the LlamaParse v2 SDK (`llama-cloud>=2.0`); the older `llama-parse` /
`llama-cloud-services` packages (Parse API v1) are deprecated.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.config import settings
from app.core.logging import get_logger
from app.ingestion.graph.schema import SourceType
from app.ingestion.vector.schema import ParsedDoc, ParsedImage

logger = get_logger(__name__)


def _client():
    """Build the LlamaCloud client (import lazily so the rest of the vector
    package imports without the SDK / an API key present)."""
    from llama_cloud import LlamaCloud

    if not settings.llama_cloud_api_key:
        raise RuntimeError(
            "LLAMA_CLOUD_API_KEY is not set; cannot call LlamaParse. "
            "Get a free-tier key at https://cloud.llamaindex.ai."
        )
    return LlamaCloud(api_key=settings.llama_cloud_api_key)


def _first_h1(markdown: str) -> str:
    for line in markdown.splitlines():
        s = line.strip()
        if s.startswith("# ") and not s.startswith("##"):
            return s[2:].strip()
    return ""


def parse_document(
    source_path: Path,
    *,
    doc_id: str,
    source_type: SourceType,
    tier: str | None = None,
    images_dir: Path | None = None,
) -> ParsedDoc:
    """Parse one document to markdown + saved figure/table images.

    `tier` overrides the configured LlamaParse tier. If `images_dir` is given,
    the saved figure/table images are downloaded there for the multimodal path.
    """
    tier = tier or settings.llama_parse_tier
    client = _client()
    logger.info(
        "llamaparse: submitting", extra={"doc_id": doc_id, "tier": tier, "path": str(source_path)}
    )

    with source_path.open("rb") as fh:
        result = client.parsing.parse(
            upload_file=(source_path.name, fh, "application/pdf"),
            tier=tier,
            version="latest",
            # Save figures/tables as images (layout crops + embedded rasters) so
            # the Tier-3 multimodal path can embed them without a re-parse.
            output_options={
                "images_to_save": ["layout", "embedded"],
                "markdown": {
                    "tables": {
                        "output_tables_as_markdown": True,
                        "merge_continued_tables": True,
                    },
                },
            },
            # Quality levers for a figure/table-heavy academic PDF.
            processing_options={
                "aggressive_table_extraction": True,
                "specialized_chart_parsing": settings.llama_parse_chart_parsing,
            },
            expand=["markdown", "items", "images_content_metadata", "metadata"],
            verbose=True,
        )

    markdown = result.markdown_full or _join_pages(result)
    images = _collect_images(result, images_dir)
    logger.info(
        "llamaparse: complete",
        extra={"doc_id": doc_id, "md_chars": len(markdown), "n_images": len(images)},
    )

    return ParsedDoc(
        doc_id=doc_id,
        source_type=source_type,
        source_path=str(source_path),
        title=_first_h1(markdown),
        markdown=markdown,
        images=images,
        parser=f"llamaparse-v2:{tier}",
        parsed_at=datetime.now(UTC).isoformat(),
    )


def _join_pages(result) -> str:
    """Fallback: stitch per-page markdown if `markdown_full` is absent."""
    md = getattr(result, "markdown", None)
    if md is None or not getattr(md, "pages", None):
        return ""
    parts = []
    for page in md.pages:
        text = getattr(page, "markdown", None)
        if text:
            parts.append(text)
    return "\n\n".join(parts)


def _collect_images(result, images_dir: Path | None) -> list[ParsedImage]:
    """Read image metadata off the result and, if `images_dir` is set, download
    each saved image from its presigned URL. Best-effort: a download failure is
    logged and skipped rather than failing the whole parse."""
    meta = getattr(result, "images_content_metadata", None)
    if meta is None or not getattr(meta, "images", None):
        return []

    if images_dir is not None:
        images_dir.mkdir(parents=True, exist_ok=True)

    out: list[ParsedImage] = []
    for img in meta.images:
        local_path: str | None = None
        url = getattr(img, "presigned_url", None)
        if images_dir is not None and url:
            try:
                resp = httpx.get(url, timeout=60)
                resp.raise_for_status()
                dest = images_dir / img.filename
                dest.write_bytes(resp.content)
                local_path = str(dest)
            except Exception as exc:  # noqa: BLE001 — keep other images
                logger.warning(
                    "image download failed",
                    extra={"filename": img.filename, "error": str(exc)},
                )
        out.append(
            ParsedImage(
                filename=img.filename,
                category=getattr(img, "category", None),
                page_number=None,
                local_path=local_path,
                content_type=getattr(img, "content_type", None),
                size_bytes=getattr(img, "size_bytes", None),
            )
        )
    return out
