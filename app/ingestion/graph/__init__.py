"""Knowledge-graph path of the ingestion pipeline (spec 5.3, 5.6).

Multi-pass, schema-constrained extraction from resume + narrative with a
manual-approve gate before an idempotent MERGE load into Neo4j:

    sources -> extractor (entity pass, relationship pass) -> resolver
            -> cypher (review table + MERGE) -> [manual approval] -> loader

Run via ``python -m app.ingestion.graph.pipeline`` (see that module).
"""
