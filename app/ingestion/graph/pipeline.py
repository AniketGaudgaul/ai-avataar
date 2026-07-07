"""Knowledge-graph ingestion pipeline + CLI (spec 5.3 end-to-end).

Two-step manual-approve gate:

    # 1. Extract from resume (+ narrative when present) and write review artifacts.
    #    Does NOT touch Neo4j.
    python -m app.ingestion.graph.pipeline

    # 2. After reading temp_data/graph_review/review.md, load the EXACT reviewed
    #    graph into Neo4j (loads from the saved JSON — no re-extraction drift).
    python -m app.ingestion.graph.pipeline --load

Artifacts written to `--out` (default `temp_data/graph_review/`):
  - proposed_graph.json  — envelope {extracted_at, model, graph} (the load source)
  - review.md            — human-readable entity/relationship tables
  - load.cypher          — the idempotent MERGE script (what --load executes)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.core.logging import setup_logging
from app.ingestion.graph import cypher
from app.ingestion.graph.extractor import extract_graph
from app.ingestion.graph.resolver import resolve
from app.ingestion.graph.schema import NodeType, ProposedGraph
from app.ingestion.sources import PROJECT_ROOT, build_marked_corpus, load_sources

DEFAULT_OUT = PROJECT_ROOT / "temp_data" / "graph_review"
GRAPH_JSON = "proposed_graph.json"
REVIEW_MD = "review.md"
LOAD_CYPHER = "load.cypher"


# --- Build (extract + resolve + write artifacts) ----------------------------

def build_proposed_graph(model: str | None = None) -> ProposedGraph:
    docs = load_sources()
    corpus = build_marked_corpus(docs)
    graph = extract_graph(corpus, model=model)
    return resolve(graph)


def write_artifacts(graph: ProposedGraph, out_dir: Path, *, model: str) -> str:
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted_at = datetime.now(UTC).isoformat()

    envelope = {
        "extracted_at": extracted_at,
        "model": model,
        "graph": graph.model_dump(mode="json"),
    }
    (out_dir / GRAPH_JSON).write_text(json.dumps(envelope, indent=2), encoding="utf-8")
    (out_dir / REVIEW_MD).write_text(cypher.render_review_table(graph), encoding="utf-8")
    (out_dir / LOAD_CYPHER).write_text(
        cypher.render_cypher_script(graph, extracted_at), encoding="utf-8"
    )
    return extracted_at


def _load_envelope(out_dir: Path) -> tuple[ProposedGraph, str]:
    path = out_dir / GRAPH_JSON
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found. Run the extract step first (without --load)."
        )
    envelope = json.loads(path.read_text(encoding="utf-8"))
    return ProposedGraph.model_validate(envelope["graph"]), envelope["extracted_at"]


# --- CLI summaries ----------------------------------------------------------

def _print_summary(graph: ProposedGraph, out_dir: Path) -> None:
    by_type: dict[str, int] = {}
    for e in graph.entities:
        by_type[e.type.value] = by_type.get(e.type.value, 0) + 1
    print("\n=== Proposed knowledge graph ===")
    print(f"Entities: {len(graph.entities)}   Relationships: {len(graph.relationships)}")
    for t in NodeType:
        if by_type.get(t.value):
            print(f"  {t.value:<12} {by_type[t.value]}")
    print(f"\nArtifacts written to: {out_dir}")
    print(f"  - {REVIEW_MD}   (read this)")
    print(f"  - {LOAD_CYPHER}")
    print(f"  - {GRAPH_JSON}")
    print("\nReview review.md, then load with:  python -m app.ingestion.graph.pipeline --load\n")


# --- Entry point ------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Build/load the career knowledge graph.")
    parser.add_argument(
        "--load",
        action="store_true",
        help="Load the already-reviewed proposed_graph.json into Neo4j (no re-extraction).",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help="Artifact directory."
    )
    parser.add_argument(
        "--model",
        default=settings.gemini_router_model,
        help="Gemini model for extraction (default: flash-lite, free-tier).",
    )
    args = parser.parse_args(argv)

    if args.load:
        # Import here so the extract step has no hard dependency on a reachable DB.
        from app.ingestion.graph.loader import graph_summary, load_graph, verify_connectivity

        graph, extracted_at = _load_envelope(args.out)
        print(f"Loading {len(graph.entities)} entities / {len(graph.relationships)} "
              f"relationships into Neo4j ({settings.neo4j_uri}) ...")
        verify_connectivity()
        result = load_graph(graph, extracted_at)
        print("\n=== Load result ===")
        print(f"  statements executed : {result.statements}")
        print(f"  constraints added   : {result.constraints_added}")
        print(f"  nodes created       : {result.nodes_created}")
        print(f"  relationships created: {result.relationships_created}")
        print(f"  properties set      : {result.properties_set}")
        if result.errors:
            print(f"  ERRORS ({len(result.errors)}):")
            for e in result.errors:
                print(f"    - {e}")
        print("\n=== Graph totals in Neo4j ===")
        for k, v in graph_summary().items():
            print(f"  {k}: {v}")
        return 1 if result.errors else 0

    # Default: extract + write review artifacts (no DB writes).
    graph = build_proposed_graph(model=args.model)
    write_artifacts(graph, args.out, model=args.model)
    _print_summary(graph, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
