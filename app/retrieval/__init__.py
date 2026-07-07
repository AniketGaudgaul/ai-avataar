"""Retrieval layer (spec 6): hybrid dense+BM25 search fused with Qdrant's native
RRF, small-to-big parent expansion, and a Neo4j graph-facts primitive.

- `vector.py` — hybrid retrieval + small-to-big + prompt-ready context assembly
- `graph.py`  — citable relationship facts for named entities (GraphRAG side)

RRF fusion is done inside Qdrant (see `ingestion.vector.store`), so there is no
hand-rolled `rrf_fusion` module.
"""
