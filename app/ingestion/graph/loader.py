"""Idempotent Neo4j load of an approved proposed graph (spec 5.3 step 6).

Executes the exact statements emitted by `cypher.build_statements` — constraints
first, then node MERGEs, then edge MERGEs — each in its own autocommit
transaction (schema and data statements can't share a transaction in Neo4j).
Because everything is MERGE-on-`id`, re-running is safe and converges.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from neo4j import GraphDatabase

from app.config import settings
from app.core.logging import get_logger
from app.ingestion.graph.cypher import build_statements
from app.ingestion.graph.schema import ProposedGraph

logger = get_logger(__name__)

# This Aura instance's database is named after its instance id, not "neo4j"
# (confirmed via `SHOW DATABASES` against the `system` db). Aura instance ids can
# vary, so this reads from settings rather than hardcoding "neo4j".
DEFAULT_DATABASE = settings.neo4j_database


@dataclass
class LoadResult:
    statements: int = 0
    constraints_added: int = 0
    nodes_created: int = 0
    relationships_created: int = 0
    properties_set: int = 0
    errors: list[str] = field(default_factory=list)


def get_driver():
    """Build a Neo4j driver from settings (Aura `neo4j+s://` URI)."""
    return GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )


def verify_connectivity() -> None:
    driver = get_driver()
    try:
        driver.verify_connectivity()
    finally:
        driver.close()


def load_statements(statements: list[str], *, database: str = DEFAULT_DATABASE) -> LoadResult:
    """Run each statement in its own autocommit transaction; tally counters."""
    result = LoadResult(statements=len(statements))
    driver = get_driver()
    try:
        with driver.session(database=database) as session:
            for stmt in statements:
                query = stmt.strip().rstrip(";")
                if not query:
                    continue
                try:
                    counters = session.run(query).consume().counters
                    result.constraints_added += counters.constraints_added
                    result.nodes_created += counters.nodes_created
                    result.relationships_created += counters.relationships_created
                    result.properties_set += counters.properties_set
                except Exception as exc:  # keep loading; report at the end
                    msg = f"{type(exc).__name__}: {exc} | stmt: {query[:120]}"
                    result.errors.append(msg)
                    logger.error("statement failed", extra={"error": msg})
    finally:
        driver.close()

    logger.info(
        "graph load complete",
        extra={
            "statements": result.statements,
            "constraints_added": result.constraints_added,
            "nodes_created": result.nodes_created,
            "relationships_created": result.relationships_created,
            "properties_set": result.properties_set,
            "errors": len(result.errors),
        },
    )
    return result


def load_graph(
    graph: ProposedGraph, extracted_at: str, *, database: str = DEFAULT_DATABASE
) -> LoadResult:
    """Load an approved proposed graph into Neo4j."""
    return load_statements(build_statements(graph, extracted_at), database=database)


def graph_summary(*, database: str = DEFAULT_DATABASE) -> dict[str, int]:
    """Node/relationship totals + per-label counts, for a post-load sanity check."""
    driver = get_driver()
    summary: dict[str, int] = {}
    try:
        with driver.session(database=database) as session:
            summary["nodes_total"] = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
            summary["relationships_total"] = session.run(
                "MATCH ()-[r]->() RETURN count(r) AS c"
            ).single()["c"]
            for record in session.run(
                "MATCH (n) UNWIND labels(n) AS l RETURN l AS label, count(*) AS c ORDER BY label"
            ):
                summary[f"label:{record['label']}"] = record["c"]
    finally:
        driver.close()
    return summary
