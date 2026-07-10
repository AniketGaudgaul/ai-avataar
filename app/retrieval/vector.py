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

Images reach an answer two ways, and the **default is not similarity search**:

1. `images_for_retrieved` (preferred) — **section anchoring**. Take the parent
   sections the text retrieval actually returned, and attach the figures that
   belong to them (`linked_section_id`). A figure inherits the prose's relevance
   judgement, so there is nothing to threshold and an irrelevant diagram cannot
   appear. Costs no embedding call.
2. `retrieve_images` — a separate, modality-filtered similarity pass, for queries
   that are *explicitly* visual ("show me the architecture diagram"). Use it when
   the router asks for it, not on every turn: RRF scores are rank-derived, so this
   always returns its top-k even for an out-of-scope query.

Either way images are never merged into the text ranking. A text query scores
images in a lower cosine band than prose, so in one combined list the right
diagram loses to every paragraph that mentions it (measured — see
`ingestion/vector/images.py`).
"""

from __future__ import annotations

from qdrant_client import QdrantClient

from app.config import settings
from app.core.logging import get_logger
from app.ingestion.vector.schema import Modality
from app.ingestion.vector.store import (
    get_client,
    get_siblings,
    hybrid_search,
    images_for_sections,
)
from app.retrieval.schema import RetrievedContext, RetrievedImage

logger = get_logger(__name__)


def retrieve(
    query: str,
    *,
    client: QdrantClient | None = None,
    limit: int = 6,
    prefetch_limit: int = 40,
    source_type: str | None = None,
    project_tag: str | None = None,
    expand_to_parent: bool = True,
) -> list[RetrievedContext]:
    """Return parent-expanded, de-duplicated contexts for a query, best-first.

    `limit` caps the number of child hits considered (and thus roughly the number
    of parent sections returned). `source_type` filters the lane (e.g. meta →
    `how_i_built_this`); `project_tag` scopes retrieval to one project's chunks."""
    client = client or get_client()
    hits = hybrid_search(
        client,
        query,
        limit=limit,
        prefetch_limit=prefetch_limit,
        source_type=source_type,
        project_tag=project_tag,
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


def images_for_retrieved(
    contexts: list[RetrievedContext],
    *,
    client: QdrantClient | None = None,
    limit: int | None = None,
) -> list[RetrievedImage]:
    """Attach the figures belonging to the sections that text retrieval returned.

    The default image path. An image's score is inherited from its parent section,
    so the list is ordered by how relevant the *prose* was and a figure can only
    appear alongside the context it actually illustrates — no score threshold to
    invent.

    This inherits the text pass's relevance *and its limits*: `retrieve` returns
    its top-k unconditionally, so an off-topic query still yields sections, and
    therefore still yields their figures. Out-of-scope queries are kept away from
    images the same way they are kept away from prose — the router sets
    `retrieval_plan=none` and this is never called."""
    if not contexts:
        return []
    client = client or get_client()
    rank = {c.parent_section_id: c.score for c in contexts}
    records = images_for_sections(client, list(rank))

    images = [
        RetrievedImage(
            chunk_id=(pl := r.payload or {}).get("chunk_id", str(r.id)),
            image_uri=pl.get("image_uri", ""),
            doc_id=pl.get("doc_id", ""),
            source_type=pl.get("source_type", ""),
            heading_path=pl.get("heading_path", ""),
            citation_label=pl.get("citation_label", ""),
            caption=pl.get("text", ""),
            score=rank.get(pl.get("linked_section_id", ""), 0.0),
            linked_section_id=pl.get("linked_section_id"),
        )
        for r in records
        if (r.payload or {}).get("image_uri")
    ]
    images.sort(key=lambda i: i.score, reverse=True)
    images = images[: limit or settings.retrieval_image_limit]
    logger.info(
        "section-anchored images",
        extra={"sections": len(rank), "images": len(images)},
    )
    return images


def retrieve_images(
    query: str,
    *,
    client: QdrantClient | None = None,
    limit: int | None = None,
    prefetch_limit: int = 20,
    source_type: str | None = None,
    project_tag: str | None = None,
) -> list[RetrievedImage]:
    """Return diagrams/figures matching the query, best-first.

    Runs the same hybrid dense+BM25+RRF machinery as `retrieve`, but filtered to
    `modality=image`. Because it is its own ranked list, the scores are only
    comparable to each other — never to text scores. Callers give images a small
    fixed budget (`retrieval_image_limit`) rather than interleaving them."""
    client = client or get_client()
    hits = hybrid_search(
        client,
        query,
        limit=limit or settings.retrieval_image_limit,
        prefetch_limit=prefetch_limit,
        source_type=source_type,
        project_tag=project_tag,
        modality=Modality.IMAGE.value,
    )

    images: list[RetrievedImage] = []
    for h in hits:
        pl = h.payload or {}
        uri = pl.get("image_uri")
        if not uri:
            continue
        images.append(
            RetrievedImage(
                chunk_id=pl.get("chunk_id", str(h.id)),
                image_uri=uri,
                doc_id=pl.get("doc_id", ""),
                source_type=pl.get("source_type", ""),
                heading_path=pl.get("heading_path", ""),
                citation_label=pl.get("citation_label", ""),
                caption=pl.get("text", ""),
                score=h.score,
                linked_section_id=pl.get("linked_section_id"),
            )
        )
    logger.info("image retrieve", extra={"query_chars": len(query), "images": len(images)})
    return images


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
