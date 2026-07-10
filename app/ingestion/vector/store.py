"""Qdrant vector store: hybrid (dense + BM25) collection, upsert, and native RRF
retrieval (spec 5.5, 6.1).

Design decision (per user): use **Qdrant's own hybrid machinery**, not a hand-rolled
BM25 + RRF. The collection carries two named vectors —
- `dense`  : Gemini Embedding 2, 1536-dim, cosine (computed by `embedder.py`)
- `bm25`   : a sparse vector Qdrant builds from raw text via `Document(model=
             "Qdrant/bm25")`, with the IDF modifier so the server does BM25 scoring
and retrieval fuses the two prefetches with `FusionQuery(fusion=RRF)` server-side.
No `rank_bm25`, no manual reciprocal-rank math.

Point ids are deterministic UUID5(chunk_id) so re-ingesting the same chunk updates
in place (idempotent), and the original `chunk_id` is kept in the payload.

**Images share this collection and this vector space** (spec 5.7): an image chunk's
`dense` vector is the image fused with its text sidecar, and its `bm25` vector is
built from that sidecar. What images do *not* share is a ranked list — text-query↔
image cosines sit in a lower band than text↔text ones (the modality gap; measured
in `images.py`), so every search takes a `modality` filter and the two are queried
separately. Mixing them in one prefetch buries the images.
"""

from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.config import settings
from app.core.assets import resolve
from app.core.logging import get_logger
from app.ingestion.vector.embedder import embed_documents, embed_image, embed_query
from app.ingestion.vector.schema import Chunk, Modality

logger = get_logger(__name__)

_UUID_NS = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000001")  # stable namespace for chunk ids


def point_id(chunk_id: str) -> str:
    return str(uuid.uuid5(_UUID_NS, chunk_id))


def get_client(*, location: str | None = None) -> QdrantClient:
    """Connect to Qdrant. `location=":memory:"` gives an ephemeral in-process store
    (used by tests); otherwise use the configured URL + API key."""
    if location is not None:
        return QdrantClient(location=location)
    return QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)


# Payload fields we filter on (small-to-big expansion, lane filtering, graph
# bridge, modality partition, section→image anchoring). Qdrant Cloud requires a
# keyword index to filter these.
_INDEXED_FIELDS = (
    "parent_section_id",
    "source_type",
    "doc_id",
    "project_tag",
    "modality",
    "linked_section_id",
)


def ensure_indexes(client: QdrantClient) -> None:
    """Create keyword payload indexes for the fields we filter on (idempotent)."""
    for field in _INDEXED_FIELDS:
        try:
            client.create_payload_index(
                collection_name=settings.qdrant_collection,
                field_name=field,
                field_schema=qm.PayloadSchemaType.KEYWORD,
            )
        except Exception as exc:  # noqa: BLE001 — already-exists is fine
            logger.debug("payload index skip", extra={"field": field, "error": str(exc)})


def ensure_collection(client: QdrantClient, *, recreate: bool = False) -> None:
    """Create the hybrid collection if absent (dense cosine + BM25 sparse w/ IDF)
    and ensure the payload indexes needed for filtering exist."""
    name = settings.qdrant_collection
    exists = client.collection_exists(name)
    if exists and recreate:
        client.delete_collection(name)
        exists = False
    if exists:
        ensure_indexes(client)
        return
    client.create_collection(
        collection_name=name,
        vectors_config={
            settings.qdrant_dense_vector: qm.VectorParams(
                size=settings.embedding_dim, distance=qm.Distance.COSINE
            )
        },
        sparse_vectors_config={
            # IDF modifier => Qdrant applies BM25's inverse-document-frequency term.
            settings.qdrant_sparse_vector: qm.SparseVectorParams(modifier=qm.Modifier.IDF)
        },
    )
    ensure_indexes(client)
    logger.info("qdrant collection created", extra={"collection": name})


def _payload(chunk: Chunk) -> dict:
    m = chunk.metadata
    return {
        "text": chunk.text,
        "chunk_id": m.chunk_id,
        "doc_id": m.doc_id,
        "source_type": m.source_type.value,
        "heading_path": m.heading_path,
        "section": m.section,
        "chunk_index": m.chunk_index,
        "parent_section_id": m.parent_section_id,
        "project_tag": m.project_tag,
        "linked_entities": m.linked_entities,
        "content_type": m.content_type.value,
        "modality": m.modality.value,
        "image_uri": m.image_uri,
        "linked_section_id": m.linked_section_id,
        "citation_label": m.citation_label,
        "content_hash": m.content_hash,
        "token_count": m.token_count,
    }


def _dense_vector(chunk: Chunk) -> list[float]:
    """Dense vector for one chunk: the raw text, or the image fused with its
    sidecar caption. Both land in the same Gemini Embedding 2 space."""
    m = chunk.metadata
    if m.modality is Modality.IMAGE:
        if not m.image_uri:
            raise ValueError(f"image chunk {m.chunk_id} has no image_uri")
        path = resolve(m.image_uri)
        if path is None:
            raise ValueError(f"image chunk {m.chunk_id} points at missing file {m.image_uri!r}")
        return embed_image(path, sidecar=chunk.text)
    return embed_documents([chunk.text])[0]


def upsert_chunks(client: QdrantClient, chunks: list[Chunk], *, batch_size: int = 32) -> int:
    """Embed (dense) + build BM25 (sparse, server-side) + upsert all chunks.

    Text and image chunks go into the same collection; only how the dense vector
    is produced differs. An image's `chunk.text` is its sidecar, so it still gets
    a real BM25 vector — otherwise images would be invisible to the sparse half
    of hybrid retrieval."""
    ensure_collection(client)
    dense_name = settings.qdrant_dense_vector
    sparse_name = settings.qdrant_sparse_vector

    # Embed text chunks in one batched pass (progress logging), images one by one.
    text_chunks = [c for c in chunks if c.metadata.modality is not Modality.IMAGE]
    dense_by_id = dict(
        zip(
            (c.metadata.chunk_id for c in text_chunks),
            embed_documents([c.text for c in text_chunks]) if text_chunks else [],
            strict=True,
        )
    )

    points: list[qm.PointStruct] = []
    for chunk in chunks:
        m = chunk.metadata
        dense = dense_by_id.get(m.chunk_id) or _dense_vector(chunk)
        points.append(
            qm.PointStruct(
                id=point_id(m.chunk_id),
                vector={
                    dense_name: dense,
                    # Qdrant/fastembed turns this text into the BM25 sparse vector.
                    sparse_name: qm.Document(text=chunk.text, model=settings.qdrant_bm25_model),
                },
                payload=_payload(chunk),
            )
        )

    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=settings.qdrant_collection, points=points[i : i + batch_size])
    n_img = sum(1 for c in chunks if c.metadata.modality is Modality.IMAGE)
    logger.info(
        "qdrant upsert complete",
        extra={"n_points": len(points), "n_text": len(text_chunks), "n_image": n_img},
    )
    return len(points)


def get_siblings(client: QdrantClient, parent_section_id: str) -> list[qm.Record]:
    """Fetch every *text* chunk sharing a `parent_section_id`, ordered by `chunk_index`.

    Concatenating these reproduces the full parent section (small-to-big) without
    storing parents separately — a parent's text is exactly its children in order.
    Restricted to `modality=text` so an image sidecar can never be spliced into
    the middle of a reconstructed prose section."""
    records, _ = client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=qm.Filter(
            must=[
                qm.FieldCondition(
                    key="parent_section_id", match=qm.MatchValue(value=parent_section_id)
                ),
                qm.FieldCondition(key="modality", match=qm.MatchValue(value=Modality.TEXT.value)),
            ]
        ),
        limit=256,
        with_payload=True,
        with_vectors=False,
    )
    return sorted(records, key=lambda r: (r.payload or {}).get("chunk_index", 0))


def delete_doc(client: QdrantClient, doc_id: str) -> None:
    """Remove every point belonging to a document.

    Point ids are `UUID5(chunk_id)`, so an upsert only overwrites chunks whose ids
    are unchanged. Any chunker change (a new split rule, a dropped section) shifts
    the `cNNNN` ids and the old points survive as orphans — stale text that still
    retrieves, and `parent_section_id`s that no longer match anything. Re-ingest is
    therefore delete-then-insert, not upsert."""
    client.delete(
        collection_name=settings.qdrant_collection,
        points_selector=qm.FilterSelector(
            filter=qm.Filter(
                must=[qm.FieldCondition(key="doc_id", match=qm.MatchValue(value=doc_id))]
            )
        ),
    )
    logger.info("qdrant doc deleted", extra={"doc_id": doc_id})


def images_for_sections(client: QdrantClient, parent_section_ids: list[str]) -> list[qm.Record]:
    """Fetch the image chunks that illustrate any of the given text sections.

    This is the *primary* way images reach an answer: a figure inherits the
    relevance of the prose section it belongs to, rather than being ranked
    against the query itself. Nothing to threshold, and an image anchored to no
    retrieved section cannot surface (see spec 6.3)."""
    if not parent_section_ids:
        return []
    records, _ = client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=qm.Filter(
            must=[
                qm.FieldCondition(
                    key="linked_section_id", match=qm.MatchAny(any=parent_section_ids)
                ),
                qm.FieldCondition(key="modality", match=qm.MatchValue(value=Modality.IMAGE.value)),
            ]
        ),
        limit=64,
        with_payload=True,
        with_vectors=False,
    )
    return records


def image_record(client: QdrantClient, chunk_id: str) -> qm.Record | None:
    """Look up one image chunk by `chunk_id`, or None if there is no such image.

    The store is the authority on which files are servable: `chunk_id` arrives from
    an API caller, but `image_uri` is read back from the payload written at ingest,
    so a caller can never steer a read at an arbitrary path. The modality check
    stops a text chunk's id from resolving at all."""
    records = client.retrieve(
        collection_name=settings.qdrant_collection,
        ids=[point_id(chunk_id)],
        with_payload=True,
        with_vectors=False,
    )
    if not records:
        return None
    payload = records[0].payload or {}
    if payload.get("modality") != Modality.IMAGE.value or not payload.get("image_uri"):
        return None
    return records[0]


def hybrid_search(
    client: QdrantClient,
    query: str,
    *,
    limit: int = 8,
    prefetch_limit: int = 40,
    source_type: str | None = None,
    project_tag: str | None = None,
    modality: str | None = Modality.TEXT.value,
) -> list[qm.ScoredPoint]:
    """Dense + BM25 hybrid retrieval fused with Qdrant's native RRF.

    `source_type` optionally restricts the lane (e.g. meta → how_i_built_this);
    `project_tag` scopes the search to one project's chunks (spec 5.5 — the tag
    matches a Neo4j `Project` node id). `modality` defaults to `text`: images are
    score-incomparable with text (see module docstring) and must be searched in
    their own pass rather than ranked alongside prose. All filters are ANDed."""
    conditions: list[qm.FieldCondition] = []
    if source_type:
        conditions.append(
            qm.FieldCondition(key="source_type", match=qm.MatchValue(value=source_type))
        )
    if project_tag:
        conditions.append(
            qm.FieldCondition(key="project_tag", match=qm.MatchValue(value=project_tag))
        )
    if modality:
        conditions.append(qm.FieldCondition(key="modality", match=qm.MatchValue(value=modality)))
    query_filter = qm.Filter(must=conditions) if conditions else None
    result = client.query_points(
        collection_name=settings.qdrant_collection,
        prefetch=[
            qm.Prefetch(
                query=embed_query(query),
                using=settings.qdrant_dense_vector,
                limit=prefetch_limit,
                filter=query_filter,
            ),
            qm.Prefetch(
                query=qm.Document(text=query, model=settings.qdrant_bm25_model),
                using=settings.qdrant_sparse_vector,
                limit=prefetch_limit,
                filter=query_filter,
            ),
        ],
        query=qm.FusionQuery(fusion=qm.Fusion.RRF),
        limit=limit,
        with_payload=True,
    )
    return result.points
