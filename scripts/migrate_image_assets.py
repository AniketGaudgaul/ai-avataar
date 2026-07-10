"""One-shot migration: make indexed figures portable across hosts.

Figures ingested before `app/core/assets.py` existed recorded an **absolute** path
from the ingesting machine (`D:\\AI Avataar Project\\temp_data\\...`). That path
does not exist inside the deployment container, where it fails silently: the
specialist is shown no figure and answers in text, and `/images/{chunk_id}` 410s.

This copies each figure's bytes into `assets/<doc_id>/<filename>` and rewrites the
Qdrant payload's `image_uri` to that relative form. It touches **payloads only** —
no vectors are recomputed, so it costs zero Gemini quota.

Idempotent: already-relative URIs are left alone.

    python -m scripts.migrate_image_assets --dry-run
    python -m scripts.migrate_image_assets
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qdrant_client import models as qm

from app.config import settings
from app.core.assets import assets_root, store_image
from app.core.logging import setup_logging
from app.ingestion.vector.schema import Modality
from app.ingestion.vector.store import get_client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Report, change nothing.")
    args = parser.parse_args()

    setup_logging()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    client = get_client()
    points, _ = client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=qm.Filter(
            must=[
                qm.FieldCondition(key="modality", match=qm.MatchValue(value=Modality.IMAGE.value))
            ]
        ),
        limit=1000,
        with_payload=True,
        with_vectors=False,
    )
    print(f"assets root : {assets_root()}")
    print(f"image chunks: {len(points)}\n")

    migrated = skipped = missing = 0
    for p in points:
        payload = p.payload or {}
        chunk_id = payload.get("chunk_id", str(p.id))
        doc_id = payload.get("doc_id", "")
        uri = payload.get("image_uri", "")

        if not uri:
            print(f"  !  {chunk_id}: no image_uri")
            missing += 1
            continue
        if not Path(uri).is_absolute():
            skipped += 1
            continue
        source = Path(uri)
        if not source.is_file():
            # Bytes are gone on this machine; nothing to copy. Re-ingest that doc.
            print(f"  !  {chunk_id}: source missing → {uri}")
            missing += 1
            continue

        if args.dry_run:
            print(f"  →  {chunk_id}: {source.name} → {doc_id}/{source.name}")
            migrated += 1
            continue

        new_uri = store_image(doc_id, source)
        client.set_payload(
            collection_name=settings.qdrant_collection,
            payload={"image_uri": new_uri},
            points=[p.id],
            wait=True,
        )
        print(f"  ✓  {chunk_id}: {new_uri}")
        migrated += 1

    verb = "would migrate" if args.dry_run else "migrated"
    print(f"\n{verb}: {migrated}   already relative: {skipped}   missing bytes: {missing}")
    return 1 if missing else 0


if __name__ == "__main__":
    raise SystemExit(main())
