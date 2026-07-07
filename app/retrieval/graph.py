"""Graph-facts retrieval (spec 6.2) — the relational side of GraphRAG.

Given entity names (e.g. from a query or from a chunk's `linked_entities`), fetch
that entity's typed relationships from Neo4j as citable facts. This is the
primitive the router will call for relational queries ("what companies has he
worked at", "what tech overlaps between X and Y") — a traversal, not a similarity
search. NL→Cypher generation is deliberately out of scope here; callers pass
resolved entity names.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.ingestion.graph.loader import DEFAULT_DATABASE, get_driver
from app.retrieval.schema import GraphFact

logger = get_logger(__name__)

# Undirected match so we get both (n)-[r]->(m) and (m)-[r]->(n); we normalize
# direction to (subject)-[rel]->(object) using startNode below.
_FACTS_CYPHER = """
UNWIND $names AS name
MATCH (n)
WHERE toLower(n.canonical_name) = name
   OR any(a IN coalesce(n.aliases, []) WHERE toLower(a) = name)
MATCH (n)-[r]-(m)
WITH startNode(r) AS s, endNode(r) AS o, r
RETURN DISTINCT
    s.canonical_name AS subject,
    type(r)          AS relation,
    o.canonical_name AS object,
    properties(r)    AS props,
    coalesce(r.source_docs, []) AS source_docs
LIMIT $limit
"""


def facts_for_entities(names: list[str], *, limit: int = 50) -> list[GraphFact]:
    """Return citable relationship facts for the given entity names (case-insensitive
    match on canonical_name or aliases)."""
    if not names:
        return []
    lowered = [n.strip().lower() for n in names if n.strip()]
    driver = get_driver()
    facts: list[GraphFact] = []
    try:
        with driver.session(database=DEFAULT_DATABASE) as session:
            for rec in session.run(_FACTS_CYPHER, names=lowered, limit=limit):
                props = {
                    k: str(v)
                    for k, v in (rec["props"] or {}).items()
                    if k not in ("source_docs", "source_span", "extracted_at", "confidence")
                }
                facts.append(
                    GraphFact(
                        subject=rec["subject"],
                        relation=rec["relation"],
                        object=rec["object"],
                        properties=props,
                        source_docs=list(rec["source_docs"] or []),
                    )
                )
    finally:
        driver.close()
    logger.info("graph facts", extra={"names": lowered, "facts": len(facts)})
    return facts
