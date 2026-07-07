"""Hybrid vector retrieval with small-to-big expansion (spec 6.1, 5.4 rule 7).

Flow:
1. Hybrid search (dense + BM25, fused by Qdrant RRF) returns the most relevant
   *child* chunks — small chunks give precision.
2. Collapse children into their parent sections: children that share a
   `parent_section_id` become one context, and each parent is reconstructed by
   stitching all its children in order (see `store.get_siblings`). The generator
   then sees the full, self-contained section rather than a narrow fragment.
3. Assemble a numbered, citation-labelled context block ready to drop into a
   prompt — each block is `[n] (citation_label)`, so the model can cite by number.
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from app.core.logging import get_logger
from app.ingestion.vector.store import get_client, get_siblings, hybrid_search
from app.retrieval.schema import RetrievedContext

logger = get_logger(__name__)


def retrieve(
    query: str,
    *,
    client: QdrantClient | None = None,
    limit: int = 6,
    prefetch_limit: int = 40,
    source_type: str | None = None,
    expand_to_parent: bool = True,
) -> list[RetrievedContext]:
    """Return parent-expanded, de-duplicated contexts for a query, best-first.

    `limit` caps the number of child hits considered (and thus roughly the number
    of parent sections returned). `source_type` filters the lane (e.g. meta →
    `how_i_built_this`)."""
    client = client or get_client()
    hits = hybrid_search(
        client, query, limit=limit, prefetch_limit=prefetch_limit, source_type=source_type
    )

    # Group hits by parent, preserving first-seen (rank) order.
    order: list[str] = []
    grouped: dict[str, dict] = {}
    for h in hits:
        pl = h.payload or {}
        pid = pl.get("parent_section_id", pl.get("chunk_id", str(h.id)))
        g = grouped.get(pid)
        if g is None:
            grouped[pid] = {
                "doc_id": pl.get("doc_id", ""),
                "source_type": pl.get("source_type", ""),
                "heading_path": pl.get("heading_path", ""),
                "citation_label": pl.get("citation_label", ""),
                "score": h.score,
                "matched": [pl.get("chunk_id", str(h.id))],
                "content_types": [pl.get("content_type", "")],
                "own_text": pl.get("text", ""),
            }
            order.append(pid)
        else:
            g["score"] = max(g["score"], h.score)
            g["matched"].append(pl.get("chunk_id", str(h.id)))
            g["content_types"].append(pl.get("content_type", ""))

    contexts: list[RetrievedContext] = []
    for pid in order:
        g = grouped[pid]
        if expand_to_parent:
            siblings = get_siblings(client, pid)
            text = "\n\n".join((s.payload or {}).get("text", "") for s in siblings).strip()
        else:
            text = g["own_text"]
        contexts.append(
            RetrievedContext(
                parent_section_id=pid,
                doc_id=g["doc_id"],
                source_type=g["source_type"],
                heading_path=g["heading_path"],
                citation_label=g["citation_label"],
                text=text or g["own_text"],
                score=g["score"],
                matched_chunk_ids=g["matched"],
                content_types=sorted(set(g["content_types"])),
            )
        )

    contexts.sort(key=lambda c: c.score, reverse=True)
    logger.info(
        "vector retrieve",
        extra={"query_chars": len(query), "hits": len(hits), "contexts": len(contexts)},
    )
    return contexts


def format_for_prompt(contexts: list[RetrievedContext]) -> tuple[str, list[str]]:
    """Render contexts as a numbered, citation-labelled block for the generator.

    Returns `(context_block, citations)` where `citations[i]` is the label for
    marker `[i+1]` — the guardrail requires every factual claim to trace to one."""
    blocks: list[str] = []
    citations: list[str] = []
    for i, c in enumerate(contexts, 1):
        citations.append(c.citation_label)
        blocks.append(f"[{i}] ({c.citation_label})\n{c.text}")
    return "\n\n".join(blocks), citations
