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

    # 4. Link figures to their captions (one vision call per image), then embed:
    python -m app.ingestion.vector.pipeline --link-images
    python -m app.ingestion.vector.pipeline --embed --query "show me the architecture diagram"

Artifacts written to `--out` (default temp_data/vector_review/):
  - chunks.json  — full ChunkedDoc envelope (text chunks + parents + image chunks)
  - chunks.md    — human-readable per-chunk review (read this), figures inlined
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.core.logging import setup_logging
from app.ingestion.graph.schema import SourceType
from app.ingestion.sources import PROJECT_ROOT
from app.ingestion.vector.chunker import chunk_parsed_doc
from app.ingestion.vector.images import build_image_chunks, link_images
from app.ingestion.vector.schema import ChunkedDoc, ContentType, ParsedDoc, ParsedImage

DEFAULT_SOURCE = PROJECT_ROOT / "temp_data" / "ECIR_24_Submission_v2.pdf"
DEFAULT_OUT = PROJECT_ROOT / "temp_data" / "vector_review"
PARSED_DIR = PROJECT_ROOT / "temp_data" / "parsed"


def _doc_id(path: Path) -> str:
    stem = path.stem.lower()
    return "".join(c if c.isalnum() else "-" for c in stem).strip("-")


def _images_from_dir(images_dir: Path) -> list[ParsedImage]:
    """Rebuild image metadata from the saved files alone.

    Used when the markdown cache predates the image sidecar cache. LlamaParse's
    filenames encode everything we still need: `page_7_table_1_v2.jpg` is a layout
    crop from page 7, `img_p7_2.png` is a raster embedded in page 7."""
    from app.ingestion.vector.images import mime_for
    from app.ingestion.vector.parser import page_from_filename

    if not images_dir.is_dir():
        return []
    out: list[ParsedImage] = []
    for p in sorted(images_dir.iterdir()):
        if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            continue
        out.append(
            ParsedImage(
                filename=p.name,
                category="layout" if p.name.startswith("page_") else "embedded",
                page_number=page_from_filename(p.name),
                local_path=str(p),
                content_type=mime_for(p),
                size_bytes=p.stat().st_size,
            )
        )
    return out


def _project_tag_exists(tag: str) -> bool:
    """Check the tag against the Neo4j project catalog before stamping it.

    A `project_tag` that matches no `Project` node makes the router's filter match
    zero chunks — retrieval silently returns nothing rather than erroring, so a
    typo here is expensive to notice later."""
    try:
        from app.retrieval.graph import list_projects

        known = {p["id"] for p in list_projects()}
    except Exception as exc:  # noqa: BLE001 — graph unreachable: warn, don't block
        print(f"Warning: could not verify project_tag against Neo4j ({exc}).", file=sys.stderr)
        return True
    if tag not in known:
        print(
            f"Unknown project_tag {tag!r}. Known Project ids: {sorted(known)}",
            file=sys.stderr,
        )
        return False
    return True


def _get_parsed_doc(
    source: Path, doc_id: str, source_type: SourceType, tier: str, reparse: bool
) -> ParsedDoc:
    """Return the parsed markdown, using the on-disk cache unless --reparse.

    The cached markdown lets the chunker be iterated with zero API calls; a real
    parse only happens on first run or --reparse (and requires LLAMA_CLOUD_API_KEY)."""
    PARSED_DIR.mkdir(parents=True, exist_ok=True)

    # Authored markdown needs no parser and no cache — it is already the artifact
    # LlamaParse would be trying to reconstruct. Read it (and its images) directly.
    if source.suffix.lower() in (".md", ".markdown"):
        from app.ingestion.vector.parser import load_markdown

        doc = load_markdown(
            source,
            doc_id=doc_id,
            source_type=source_type,
            raster_dir=PARSED_DIR / f"{doc_id}_images",
        )
        print(
            f"Loaded authored markdown: {source.name} "
            f"({len(doc.markdown)} chars, {len(doc.images)} inline images)"
        )
        return doc

    cache_md = PARSED_DIR / f"{doc_id}.md"
    cache_images = PARSED_DIR / f"{doc_id}.images.json"
    images_dir = PARSED_DIR / f"{doc_id}_images"

    if cache_md.exists() and not reparse:
        markdown = cache_md.read_text(encoding="utf-8")
        print(f"Using cached markdown: {cache_md}  ({len(markdown)} chars)")
        from app.ingestion.vector.parser import _first_h1

        if cache_images.exists():
            images = [
                ParsedImage.model_validate(i)
                for i in json.loads(cache_images.read_text(encoding="utf-8"))
            ]
        else:
            images = _images_from_dir(images_dir)
        return ParsedDoc(
            doc_id=doc_id,
            source_type=source_type,
            source_path=str(source),
            title=_first_h1(markdown),
            markdown=markdown,
            images=images,
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
    cache_images.write_text(
        json.dumps([i.model_dump() for i in doc.images], indent=2), encoding="utf-8"
    )
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

    if c.image_chunks:
        lines.append(f"\n---\n\n## Image chunks ({len(c.image_chunks)})\n")
        lines.append("Each is embedded as `[sidecar, image]` fused into one vector.\n")
        for ch in c.image_chunks:
            m = ch.metadata
            img = c.images[m.chunk_index] if m.chunk_index < len(c.images) else None
            method = img.link_method if img else "?"
            lines.append(f"\n### `{m.chunk_id}` · link=`{method}` · `{m.image_uri}`")
            lines.append(f"**Cite:** {m.citation_label}")
            lines.append(f"**Linked section:** `{m.linked_section_id or '—'}`")
            lines.append(f"**Sidecar:** {ch.text}")
            if m.image_uri:
                lines.append(f"\n![{m.section}]({Path(m.image_uri).as_posix()})")
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
          f"   Image chunks: {len(c.image_chunks)}")
    print(f"Tokens   : min {min(toks)}  median {int(statistics.median(toks))}  max {max(toks)}"
          f"   (>400 tok: {over})")
    print("By content_type:")
    for t in ContentType:
        if by_type.get(t.value):
            print(f"  {t.value:<16} {by_type[t.value]}")
    if c.images:
        by_link: dict[str, int] = {}
        for img in c.images:
            by_link[img.link_method] = by_link.get(img.link_method, 0) + 1
        print("Images by link_method:")
        for k, v in sorted(by_link.items()):
            print(f"  {k:<16} {v}")
    print(f"\nArtifacts written to: {out_dir}")
    print("  - chunks.md   (read this)")
    print("  - chunks.json")


# --- Entry point ------------------------------------------------------------

def _embed_and_query(
    out_dir: Path,
    *,
    do_embed: bool,
    images_only: bool,
    text_only: bool,
    replace: bool,
    query: str | None,
) -> int:
    """Load reviewed chunks.json → embed + upsert into Qdrant, then optionally run a
    hybrid search. Imported lazily so parse/chunk needs no Qdrant/embedder deps."""
    from app.config import settings
    from app.ingestion.vector.schema import ChunkedDoc
    from app.ingestion.vector.store import delete_doc, get_client, hybrid_search, upsert_chunks
    from app.retrieval.vector import retrieve_images

    path = out_dir / "chunks.json"
    if not path.exists():
        print(f"{path} not found. Run the chunking step first.", file=sys.stderr)
        return 2
    chunked = ChunkedDoc.model_validate_json(path.read_text(encoding="utf-8"))
    client = get_client()

    if do_embed:
        todo = []
        if not images_only:
            todo += chunked.chunks
        if not text_only:
            todo += chunked.image_chunks
        if not todo:
            print("Nothing to embed for the given flags.", file=sys.stderr)
            return 2
        if replace and not (images_only or text_only):
            print(f"Deleting existing points for doc_id={chunked.doc_id!r} (stale chunk ids) ...")
            delete_doc(client, chunked.doc_id)
        n_img = sum(1 for c in todo if c.metadata.image_uri)
        print(
            f"Embedding {len(todo) - n_img} text + {n_img} image chunks "
            f"(Gemini Embedding 2, {settings.embedding_dim}-dim) → Qdrant collection "
            f"'{settings.qdrant_collection}' at {settings.qdrant_url} ..."
        )
        n = upsert_chunks(client, todo)
        print(f"Upserted {n} points (dense + BM25 sparse).")

    if query:
        print(f"\n=== Text hybrid search (RRF): {query!r} ===")
        for i, p in enumerate(hybrid_search(client, query, limit=5), 1):
            pl = p.payload or {}
            print(f"{i}. [{p.score:.4f}] {pl.get('content_type')} · {pl.get('heading_path')}")
            print(f"   {pl.get('text','')[:140].strip()!r}")

        print(f"\n=== Image search (separate modality pass): {query!r} ===")
        images = retrieve_images(query, client=client, limit=3)
        if not images:
            print("  (no image chunks in the collection)")
        for i, img in enumerate(images, 1):
            print(f"{i}. [{img.score:.4f}] {img.citation_label}")
            print(f"   {Path(img.image_uri).name}  ·  {img.caption[:110]}")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Parse + chunk a document for the vector store.")
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE, help="Document to ingest.")
    parser.add_argument(
        "--source-type", default=SourceType.PAPER.value,
        choices=[s.value for s in SourceType], help="Provenance tag (spec 5.5).",
    )
    parser.add_argument(
        "--project-tag", default=None,
        help="Canonical Neo4j Project id to stamp on every chunk (spec 5.5), e.g. "
             "'agentic-rag-presentation-generator'. Activates the router's project filter.",
    )
    parser.add_argument("--tier", default=None, help="LlamaParse tier override.")
    parser.add_argument("--reparse", action="store_true", help="Force a fresh LlamaParse call.")
    parser.add_argument(
        "--link-images", action="store_true",
        help="Use a vision model to match each figure to its caption. Needed for PDFs, "
             "whose images arrive as a flat list; authored markdown with inline "
             "![caption](path) refs links exactly and for free without this flag.",
    )
    parser.add_argument(
        "--image-links", type=Path, default=None,
        help="JSON of reviewed image→caption links ({\"page_7_table_1_v2.jpg\": \"Figure 2\"}); "
             "\"\" forces unlinked. Overrides any inferred match. "
             "Defaults to <out>/image_links.json when present.",
    )
    parser.add_argument(
        "--embed", action="store_true",
        help="Embed the reviewed chunks.json + upsert into Qdrant (hybrid dense+BM25). "
             "Runs after you've reviewed chunks.md.",
    )
    parser.add_argument(
        "--images-only", action="store_true", help="With --embed, upsert only the image chunks.",
    )
    parser.add_argument(
        "--text-only", action="store_true", help="With --embed, upsert only the text chunks.",
    )
    parser.add_argument(
        "--replace", action=argparse.BooleanOptionalAction, default=True,
        help="Delete the doc's existing points before upserting. On by default: chunk ids "
             "shift whenever the chunker changes, so plain upsert would orphan stale points. "
             "Skipped for --images-only/--text-only (which are partial loads).",
    )
    parser.add_argument(
        "--query", default=None,
        help="With --embed (or alone), run a hybrid search for this string as a sanity check. "
             "Searches text and images in separate modality passes.",
    )
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="Artifact directory.")
    args = parser.parse_args(argv)

    if args.embed or args.query:
        return _embed_and_query(
            args.out,
            do_embed=args.embed,
            images_only=args.images_only,
            text_only=args.text_only,
            replace=args.replace,
            query=args.query,
        )

    if not args.source.exists():
        print(f"Source not found: {args.source}", file=sys.stderr)
        return 2

    doc_id = _doc_id(args.source)
    source_type = SourceType(args.source_type)
    from app.config import settings

    tier = args.tier or settings.llama_parse_tier
    doc = _get_parsed_doc(args.source, doc_id, source_type, tier, args.reparse)
    chunked = chunk_parsed_doc(doc)

    # Multimodal path (spec 5.7): link each figure to its section, then build the
    # image chunks whose sidecar carries caption + heading breadcrumb.
    overrides_path = args.image_links or (args.out / "image_links.json")
    overrides = {}
    if overrides_path.exists():
        raw = json.loads(overrides_path.read_text(encoding="utf-8"))
        # "_"-prefixed keys are comments, not image filenames.
        overrides = {k: v for k, v in raw.items() if not k.startswith("_")}
    if overrides:
        print(f"Applying {len(overrides)} reviewed image link(s) from {overrides_path}")
    linked = link_images(doc, use_vision=args.link_images, overrides=overrides)
    chunked.images = linked
    chunked.image_chunks = build_image_chunks(doc, linked)

    # `project_tag` must match a Neo4j `Project` node id (spec 5.5) — it is what
    # scopes retrieval to one project and what the router resolves a query to.
    if args.project_tag:
        if not _project_tag_exists(args.project_tag):
            return 2
        for chunk in (*chunked.chunks, *chunked.image_chunks):
            chunk.metadata.project_tag = args.project_tag
        print(f"Stamped project_tag='{args.project_tag}' on {len(chunked.chunks)} text "
              f"+ {len(chunked.image_chunks)} image chunks")

    _write_artifacts(chunked, args.out)
    _print_summary(chunked, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
