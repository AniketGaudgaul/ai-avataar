"""Vector-store ingestion path (spec 5.4-5.5): parse -> section-aware chunk.

Embedding (Gemini Embedding 2) and the Qdrant load are deliberately NOT part of
this package yet -- this stage stops at structural chunks so the chunk quality
can be reviewed before any embedding quota is spent.
"""
