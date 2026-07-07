"""Quick manual retrieval check:

    python -m app.retrieval.cli "how does MedSumm fuse text and images"
    python -m app.retrieval.cli --source-type paper --limit 4 "the MMCQS dataset"
    python -m app.retrieval.cli --graph "Aniket Gaudgaul"      # graph facts for an entity
"""

from __future__ import annotations

import argparse
import sys

from app.core.logging import setup_logging


def main(argv: list[str] | None = None) -> int:
    # Heading breadcrumbs use "▸", which the default Windows console (cp1252)
    # can't encode — force UTF-8 so the CLI never dies on a print.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    setup_logging()
    p = argparse.ArgumentParser(description="Exercise the retrieval layer.")
    p.add_argument("query", help="Query string (or entity name with --graph).")
    p.add_argument("--limit", type=int, default=6, help="Max child hits / parent contexts.")
    p.add_argument("--source-type", default=None, help="Restrict lane (resume|paper|...).")
    p.add_argument("--no-expand", action="store_true", help="Skip small-to-big parent expansion.")
    p.add_argument("--graph", action="store_true", help="Query the graph for the entity instead.")
    p.add_argument("--show-text", action="store_true", help="Print full context text, not preview.")
    args = p.parse_args(argv)

    if args.graph:
        from app.retrieval.graph import facts_for_entities

        facts = facts_for_entities([args.query])
        print(f"\n=== Graph facts for {args.query!r} ({len(facts)}) ===")
        for f in facts:
            src = f", src={f.source_docs}" if f.source_docs else ""
            print(f"  {f.as_sentence()}{src}")
        return 0

    from app.retrieval.vector import format_for_prompt, retrieve

    contexts = retrieve(
        args.query,
        limit=args.limit,
        source_type=args.source_type,
        expand_to_parent=not args.no_expand,
    )
    print(f"\n=== Retrieved {len(contexts)} contexts for {args.query!r} ===")
    for i, c in enumerate(contexts, 1):
        print(f"\n[{i}] score={c.score:.4f}  {c.content_types}  {c.heading_path}")
        print(f"    cite: {c.citation_label}  (matched {len(c.matched_chunk_ids)} chunk(s))")
        body = c.text if args.show_text else c.text[:220].replace("\n", " ") + "…"
        print(f"    {body}")

    block, cites = format_for_prompt(contexts)
    print(f"\n--- prompt context is {len(block)} chars over {len(cites)} citations ---")
    return 0


if __name__ == "__main__":
    sys.exit(main())
