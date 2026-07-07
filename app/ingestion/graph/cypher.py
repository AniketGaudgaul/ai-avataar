"""Render the proposed graph as (a) a human-readable review table and (b)
idempotent Cypher for the manual-approve gate (spec 5.3 steps 5-6).

Everything is `MERGE` (not `CREATE`) keyed on `id`, so re-runs are idempotent.
Every node and edge carries provenance (`source_docs`, `source_spans` /
`source_span`, `extracted_at`) so the guardrail can later cite graph facts
(spec 5.6). The `.cypher` file is what gets reviewed *and* what gets loaded —
the loader executes these exact statements, so nothing hidden slips in.
"""

from __future__ import annotations

from app.ingestion.graph.schema import (
    ExtractedEntity,
    ExtractedRelationship,
    NodeType,
    ProposedGraph,
)

# Node base-property names we set explicitly; custom props may not overwrite them.
_RESERVED = {"id", "canonical_name", "aliases", "source_docs", "source_spans", "extracted_at"}


# --- Cypher literal helpers -------------------------------------------------

def _cy_str(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace("'", "\\'").replace("\r", "").replace("\n", "\\n")
    return f"'{escaped}'"


def _cy_list(items: list[str]) -> str:
    return "[" + ", ".join(_cy_str(x) for x in items) + "]"


def _cy_val(v: object) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return _cy_str(str(v))


def _prop_key(key: str) -> str:
    return "`" + key.replace("`", "") + "`"


def _source_docs(sources) -> list[str]:
    # dedupe, preserve order
    return list(dict.fromkeys(s.value for s in sources))


# --- Statement builders -----------------------------------------------------

def constraint_statements() -> list[str]:
    """One uniqueness constraint per node label so MERGE-on-id is safe + fast."""
    return [
        f"CREATE CONSTRAINT {t.value.lower()}_id IF NOT EXISTS "
        f"FOR (n:{t.value}) REQUIRE n.id IS UNIQUE;"
        for t in NodeType
    ]


def node_statement(entity: ExtractedEntity, extracted_at: str) -> str:
    assignments = [
        f"n.canonical_name = {_cy_str(entity.canonical_name)}",
        f"n.aliases = {_cy_list(entity.aliases)}",
        f"n.source_docs = {_cy_list(_source_docs(entity.sources))}",
        f"n.source_spans = {_cy_list(entity.source_spans)}",
        f"n.extracted_at = {_cy_str(extracted_at)}",
    ]
    for k, v in entity.property_dict().items():
        if k in _RESERVED:
            continue
        assignments.append(f"n.{_prop_key(k)} = {_cy_val(v)}")
    set_clause = ",\n    ".join(assignments)
    return (
        f"MERGE (n:{entity.type.value} {{id: {_cy_str(entity.id)}}})\n"
        f"SET {set_clause};"
    )


def edge_statement(
    edge: ExtractedRelationship,
    entities: dict[str, ExtractedEntity],
    extracted_at: str,
) -> str:
    src = entities[edge.source_id]
    tgt = entities[edge.target_id]
    assignments = [
        f"r.confidence = {edge.confidence}",
        f"r.source_docs = {_cy_list(_source_docs(edge.sources))}",
        f"r.source_span = {_cy_str(edge.source_span)}",
        f"r.extracted_at = {_cy_str(extracted_at)}",
    ]
    for k, v in edge.property_dict().items():
        assignments.append(f"r.{_prop_key(k)} = {_cy_val(v)}")
    set_clause = ",\n    ".join(assignments)
    return (
        f"MATCH (a:{src.type.value} {{id: {_cy_str(edge.source_id)}}}), "
        f"(b:{tgt.type.value} {{id: {_cy_str(edge.target_id)}}})\n"
        f"MERGE (a)-[r:{edge.type.value}]->(b)\n"
        f"SET {set_clause};"
    )


def build_statements(graph: ProposedGraph, extracted_at: str) -> list[str]:
    """All statements in load order: constraints, then nodes, then edges."""
    by_id = graph.entity_by_id()
    stmts = list(constraint_statements())
    stmts += [node_statement(e, extracted_at) for e in graph.entities]
    stmts += [edge_statement(e, by_id, extracted_at) for e in graph.relationships]
    return stmts


def render_cypher_script(graph: ProposedGraph, extracted_at: str) -> str:
    """The full, commented, copy-pasteable `.cypher` review/load artifact."""
    by_id = graph.entity_by_id()
    parts = [
        "// AI Avatar — proposed knowledge graph (idempotent MERGE load)",
        f"// extracted_at: {extracted_at}",
        f"// {len(graph.entities)} entities, {len(graph.relationships)} relationships",
        "",
        "// --- Constraints ---",
        *constraint_statements(),
        "",
        "// --- Nodes ---",
    ]
    parts += [node_statement(e, extracted_at) + "\n" for e in graph.entities]
    parts += ["// --- Relationships ---"]
    parts += [edge_statement(e, by_id, extracted_at) + "\n" for e in graph.relationships]
    return "\n".join(parts) + "\n"


# --- Human review table -----------------------------------------------------

def _fmt_props(d: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in d.items()) if d else ""


def render_review_table(graph: ProposedGraph) -> str:
    """Markdown tables Aniket reads before approving the load (spec 5.3 step 5)."""
    by_id = graph.entity_by_id()
    lines: list[str] = []
    lines.append("# Proposed Knowledge Graph — review before load\n")
    lines.append(
        f"**{len(graph.entities)} entities, {len(graph.relationships)} relationships.** "
        "Approve by running the pipeline with `--load`.\n"
    )

    # Entities grouped by type.
    lines.append("## Entities\n")
    lines.append("| Type | id | Canonical name | Aliases | Properties | Sources |")
    lines.append("|---|---|---|---|---|---|")
    for t in NodeType:
        for e in sorted(graph.entities, key=lambda x: x.id):
            if e.type != t:
                continue
            lines.append(
                f"| {e.type.value} | `{e.id}` | {e.canonical_name} | "
                f"{', '.join(e.aliases)} | {_fmt_props(e.property_dict())} | "
                f"{', '.join(_source_docs(e.sources))} |"
            )

    # Relationships.
    lines.append("\n## Relationships\n")
    lines.append("| Source | Edge | Target | Properties | Conf | Sources |")
    lines.append("|---|---|---|---|---|---|")
    for r in graph.relationships:
        src = by_id[r.source_id].canonical_name
        tgt = by_id[r.target_id].canonical_name
        lines.append(
            f"| {src} | {r.type.value} | {tgt} | {_fmt_props(r.property_dict())} | "
            f"{r.confidence:.2f} | {', '.join(_source_docs(r.sources))} |"
        )
    return "\n".join(lines) + "\n"
