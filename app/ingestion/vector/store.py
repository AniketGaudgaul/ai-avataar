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
"""

from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client import models as qm

from app.config import settings
from app.core.logging import get_logger
from app.ingestion.vector.embedder import embed_documents, embed_query
from app.ingestion.vector.schema import Chunk

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
# bridge). Qdrant Cloud requires a keyword index to filter these.
_INDEXED_FIELDS = ("parent_section_id", "source_type", "doc_id", "project_tag")


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
        "citation_label": m.citation_label,
        "content_hash": m.content_hash,
        "token_count": m.token_count,
    }


def upsert_chunks(client: QdrantClient, chunks: list[Chunk], *, batch_size: int = 32) -> int:
    """Embed (dense) + build BM25 (sparse, server-side) + upsert all chunks."""
    ensure_collection(client)
    dense_name = settings.qdrant_dense_vector
    sparse_name = settings.qdrant_sparse_vector
    dense_vectors = embed_documents([c.text for c in chunks])

    points: list[qm.PointStruct] = []
    for chunk, dense in zip(chunks, dense_vectors, strict=True):
        points.append(
            qm.PointStruct(
                id=point_id(chunk.metadata.chunk_id),
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
    logger.info("qdrant upsert complete", extra={"n_points": len(points)})
    return len(points)


def get_siblings(client: QdrantClient, parent_section_id: str) -> list[qm.Record]:
    """Fetch every chunk sharing a `parent_section_id`, ordered by `chunk_index`.

    Concatenating these reproduces the full parent section (small-to-big) without
    storing parents separately — a parent's text is exactly its children in order."""
    records, _ = client.scroll(
        collection_name=settings.qdrant_collection,
        scroll_filter=qm.Filter(
            must=[
                qm.FieldCondition(
                    key="parent_section_id", match=qm.MatchValue(value=parent_section_id)
                )
            ]
        ),
        limit=256,
        with_payload=True,
        with_vectors=False,
    )
    return sorted(records, key=lambda r: (r.payload or {}).get("chunk_index", 0))


def hybrid_search(
    client: QdrantClient,
    query: str,
    *,
    limit: int = 8,
    prefetch_limit: int = 40,
    source_type: str | None = None,
) -> list[qm.ScoredPoint]:
    """Dense + BM25 hybrid retrieval fused with Qdrant's native RRF.

    `source_type` optionally restricts the lane (e.g. meta → how_i_built_this)."""
    query_filter = None
    if source_type:
        query_filter = qm.Filter(
            must=[qm.FieldCondition(key="source_type", match=qm.MatchValue(value=source_type))]
        )
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
