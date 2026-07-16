"""Stamp `project_tag` onto the per-project sections of the narrative doc.

The career-narrative doc is multi-project, so it was ingested with no
`project_tag` (the pipeline tags a whole doc at once). That left the coverage-gap
projects — Dreambrush, AskYarnit, Humanizer, Concept-to-Catwalk,
LLM-Cost-Optimization — reachable only as one untagged narrative chunk each, so a
`project_tag` filter matched nothing and retrieval fell back to unfiltered,
burying the right chunk under other projects' KB pages (eval I5).

This backfills the tag on the sections whose heading names exactly one known
project (via the catalog's distinctive-token matcher), using Qdrant `set_payload`
— no re-embedding. Idempotent: re-running only re-asserts the same tags.

    python -m app.ingestion.vector.retag_narrative            # dry run (prints plan)
    python -m app.ingestion.vector.retag_narrative --apply    # write to Qdrant
"""

from __future__ import annotations

import argparse
import sys

import re

from qdrant_client import models

from app.agents.catalog import match_projects
from app.config import settings
from app.ingestion.vector.store import get_client

NARRATIVE_SOURCE_TYPE = "narrative"

# The narrative sections are titled "<crumb> ▸ Product App: Dreambrush" /
# "… ▸ Client Project: Product Discovery". Keep only the leaf and drop the
# structural "Product App:" / "Client Project:" prefix so the generic word
# "Product" can't match the Product-Discovery project on a Dreambrush/Humanizer
# section. What remains is the project's own name.
_STRUCT_PREFIX_RE = re.compile(r"^(product app|client project)\s*:\s*", re.IGNORECASE)


def _section_name(heading_path: str) -> str:
    leaf = heading_path.rsplit("▸", 1)[-1].strip()
    return _STRUCT_PREFIX_RE.sub("", leaf).strip()


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill project_tag on narrative sections.")
    ap.add_argument("--apply", action="store_true", help="Write to Qdrant (default: dry run).")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    client = get_client()
    points, _ = client.scroll(
        settings.qdrant_collection,
        scroll_filter=models.Filter(
            must=[models.FieldCondition(
                key="source_type", match=models.MatchValue(value=NARRATIVE_SOURCE_TYPE)
            )]
        ),
        limit=200,
        with_payload=True,
    )

    # Group point ids by the single project their heading names (skip ambiguous /
    # general sections like Overview, Technical Toolkit, Common Threads).
    plan: dict[str, list] = {}
    for p in points:
        pl = p.payload or {}
        heading = pl.get("heading_path", "")
        matched = match_projects(_section_name(heading))
        chunk_id = pl.get("chunk_id", str(p.id))
        if len(matched) != 1:
            print(f"  skip  {chunk_id:16} ({len(matched)} projects) {heading[:70]}")
            continue
        tag = next(iter(matched))
        already = pl.get("project_tag")
        flag = " [already]" if already == tag else ""
        print(f"  tag   {chunk_id:16} -> {tag}{flag}   {heading[:60]}")
        plan.setdefault(tag, []).append(p.id)

    if not args.apply:
        print(f"\nDry run — {sum(len(v) for v in plan.values())} chunk(s) across "
              f"{len(plan)} project(s). Re-run with --apply to write.")
        return

    for tag, ids in plan.items():
        client.set_payload(
            settings.qdrant_collection, payload={"project_tag": tag}, points=ids, wait=True
        )
    print(f"\nApplied project_tag to {sum(len(v) for v in plan.values())} narrative chunk(s).")


if __name__ == "__main__":
    main()
