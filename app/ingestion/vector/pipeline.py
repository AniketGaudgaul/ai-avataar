"""Vector-path ingestion pipeline + CLI — parse → chunk → review (spec 5.4).

Stops at chunks on purpose: embedding (Gemini Embedding 2) and the Qdrant load
come after the chunks are reviewed. Mirrors the graph pipeline's parse-once /
iterate-on-the-artifact discipline so we never re-spend LlamaParse credits just
to tweak chunking:

    # 1. Parse (LlamaParse v2, cached to temp_data/parsed/) + chunk + write review.
    python -m app.ingestion.vector.pipeline

    # 2. Re-chunk from the CACHED markdown after tuning the chunker (no API call):
    python -m app.ingestion.vector.pipeline            # cache hit, no re-parse

    # 3. Force a fresh parse (spends credits):
    python -m app.ingestion.vector.pipeline --reparse

Artifacts written to `--out` (default temp_data/vector_review/):
  - chunks.json  — full ChunkedDoc envelope (chunks + parent sections + images)
  - chunks.md    — human-readable per-chunk review (read this)
"""

from __future__ import annotations

import argparse
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import setup_logging
from app.ingestion.graph.schema import SourceType
from app.ingestion.sources import PROJECT_ROOT
from app.ingestion.vector.chunker import chunk_parsed_doc
from app.ingestion.vector.schema import ChunkedDoc, ContentType, ParsedDoc

DEFAULT_SOURCE = PROJECT_ROOT / "temp_data" / "ECIR_24_Submission_v2.pdf"
DEFAULT_OUT = PROJECT_ROOT / "temp_data" / "vector_review"
PARSED_DIR = PROJECT_ROOT / "temp_data" / "parsed"


def _doc_id(path: Path) -> str:
    stem = path.stem.lower()
    return "".join(c if c.isalnum() else "-" for c in stem).strip("-")


def _get_parsed_doc(
    source: Path, doc_id: str, source_type: SourceType, tier: str, reparse: bool
) -> ParsedDoc:
    """Return the parsed markdown, using the on-disk cache unless --reparse.

    The cached markdown lets the chunker be iterated with zero API calls; a real
    parse only happens on first run or --reparse (and requires LLAMA_CLOUD_API_KEY)."""
    PARSED_DIR.mkdir(parents=True, exist_ok=True)
    cache_md = PARSED_DIR / f"{doc_id}.md"
    images_dir = PARSED_DIR / f"{doc_id}_images"

    if cache_md.exists() and not reparse:
        markdown = cache_md.read_text(encoding="utf-8")
        print(f"Using cached markdown: {cache_md}  ({len(markdown)} chars)")
        from app.ingestion.vector.parser import _first_h1

        return ParsedDoc(
            doc_id=doc_id,
            source_type=source_type,
            source_path=str(source),
            title=_first_h1(markdown),
            markdown=markdown,
            parser="cache",
            parsed_at=datetime.now(UTC).isoformat(),
        )

    # Fresh parse (imports the SDK lazily; needs the API key).
    from app.ingestion.vector.parser import parse_document

    print(f"Parsing {source.name} via LlamaParse v2 (tier={tier}) ...")
    doc = parse_document(
        source, doc_id=doc_id, source_type=source_type, tier=tier, images_dir=images_dir
    )
    cache_md.write_text(doc.markdown, encoding="utf-8")
    print(f"Cached markdown → {cache_md}  ({len(doc.markdown)} chars, {len(doc.images)} images)")
    return doc


# --- Review artifacts -------------------------------------------------------

def _write_artifacts(chunked: ChunkedDoc, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chunks.json").write_text(
        chunked.model_dump_json(indent=2), encoding="utf-8"
    )
    (out_dir / "chunks.md").write_text(_render_review(chunked), encoding="utf-8")


def _render_review(c: ChunkedDoc) -> str:
    lines: list[str] = [
        f"# Chunk review — {c.title or c.doc_id}\n",
        f"**{len(c.chunks)} chunks**, {len(c.parent_sections)} parent sections, "
        f"{len(c.images)} images.  Parser: `{c.parser}`.  source_type: `{c.source_type.value}`.\n",
        "Review chunk boundaries/sizes here before embedding.\n",
    ]
    for ch in c.chunks:
        m = ch.metadata
        lines.append(
            f"\n---\n\n### `{m.chunk_id}` · {m.content_type.value} · {m.token_count} tok "
            f"· parent `{m.parent_section_id}`"
        )
        lines.append(f"**Path:** {m.heading_path}")
        lines.append(f"**Cite:** {m.citation_label}\n")
        lines.append("```text")
        lines.append(ch.text)
        lines.append("```")
    return "\n".join(lines) + "\n"


def _print_summary(c: ChunkedDoc, out_dir: Path) -> None:
    toks = [ch.metadata.token_count for ch in c.chunks] or [0]
    by_type: dict[str, int] = {}
    for ch in c.chunks:
        k = ch.metadata.content_type.value
        by_type[k] = by_type.get(k, 0) + 1
    over = sum(1 for t in toks if t > 400)

    print("\n=== Chunking summary ===")
    print(f"Document : {c.title or c.doc_id}  ({c.source_type.value})")
    print(f"Chunks   : {len(c.chunks)}   Parent sections: {len(c.parent_sections)}"
          f"   Images: {len(c.images)}")
    print(f"Tokens   : min {min(toks)}  median {int(statistics.median(toks))}  max {max(toks)}"
          f"   (>400 tok: {over})")
    print("By content_type:")
    for t in ContentType:
        if by_type.get(t.value):
            print(f"  {t.value:<16} {by_type[t.value]}")
    print(f"\nArtifacts written to: {out_dir}")
    print("  - chunks.md   (read this)")
    print("  - chunks.json")


# --- Entry point ------------------------------------------------------------

def _embed_and_query(out_dir: Path, *, do_embed: bool, query: str | None) -> int:
    """Load reviewed chunks.json → embed + upsert into Qdrant, then optionally run a
    hybrid search. Imported lazily so parse/chunk needs no Qdrant/embedder deps."""
    from app.config import settings
    from app.ingestion.vector.schema import ChunkedDoc
    from app.ingestion.vector.store import get_client, hybrid_search, upsert_chunks

    path = out_dir / "chunks.json"
    if not path.exists():
        print(f"{path} not found. Run the chunking step first.", file=sys.stderr)
        return 2
    chunked = ChunkedDoc.model_validate_json(path.read_text(encoding="utf-8"))
    client = get_client()

    if do_embed:
        print(
            f"Embedding {len(chunked.chunks)} chunks (Gemini Embedding 2, "
            f"{settings.embedding_dim}-dim) → Qdrant hybrid collection "
            f"'{settings.qdrant_collection}' at {settings.qdrant_url} ..."
        )
        n = upsert_chunks(client, chunked.chunks)
        print(f"Upserted {n} points (dense + BM25 sparse).")

    if query:
        print(f"\n=== Hybrid search (RRF): {query!r} ===")
        for i, p in enumerate(hybrid_search(client, query, limit=5), 1):
            pl = p.payload or {}
            print(f"{i}. [{p.score:.4f}] {pl.get('content_type')} · {pl.get('heading_path')}")
            print(f"   {pl.get('text','')[:140].strip()!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Parse + chunk a document for the vector store.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Document to ingest.")
    parser.add_argument(
        "--source-type", default=SourceType.PAPER.value,
        choices=[s.value for s in SourceType], help="Provenance tag (spec 5.5).",
    )
    parser.add_argument("--tier", default=None, help="LlamaParse tier override.")
    parser.add_argument("--reparse", action="store_true", help="Force a fresh LlamaParse call.")
    parser.add_argument(
        "--embed", action="store_true",
        help="Embed the reviewed chunks.json + upsert into Qdrant (hybrid dense+BM25). "
             "Runs after you've reviewed chunks.md.",
    )
    parser.add_argument(
        "--query", default=None,
        help="With --embed (or alone), run a hybrid search for this string as a sanity check.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Artifact directory.")
    args = parser.parse_args(argv)

    if args.embed or args.query:
        return _embed_and_query(args.out, do_embed=args.embed, query=args.query)

    if not args.source.exists():
        print(f"Source not found: {args.source}", file=sys.stderr)
        return 2

    doc_id = _doc_id(args.source)
    source_type = SourceType(args.source_type)
    from app.config import settings

    tier = args.tier or settings.llama_parse_tier
    doc = _get_parsed_doc(args.source, doc_id, source_type, tier, args.reparse)
    chunked = chunk_parsed_doc(doc)
    _write_artifacts(chunked, args.out)
    _print_summary(chunked, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
