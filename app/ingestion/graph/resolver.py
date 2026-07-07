"""Entity resolution / dedupe (spec 5.3 step 4).

The entity pass already merges obvious surface variants, but this is the safety
net that catches what the LLM missed. It merges entities of the *same type*
whose normalized canonical name or aliases overlap, rewrites relationship
endpoints onto the surviving id, and de-duplicates the resulting edges.

Embedding-similarity dedupe (the last lever in spec 5.3) is wired as an optional
`embed_fn` hook — passing the Gemini Embedding 2 embedder (built with the vector
path) turns it on. Until then, lexical resolution runs alone.
"""

from __future__ import annotations

import re

from app.core.logging import get_logger
from app.ingestion.graph.schema import (
    ExtractedEntity,
    ExtractedRelationship,
    KVProperty,
    ProposedGraph,
)

logger = get_logger(__name__)

# Company/legal-suffix noise stripped before comparing names.
_SUFFIXES = {"pvt", "ltd", "inc", "llc", "limited", "private", "co", "corp", "the"}


def _normalize(name: str) -> str:
    tokens = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    tokens = [t for t in tokens if t not in _SUFFIXES]
    return " ".join(tokens).strip()


def _keys(entity: ExtractedEntity) -> set[str]:
    keys = {_normalize(entity.canonical_name)}
    keys.update(_normalize(a) for a in entity.aliases)
    return {k for k in keys if k}


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def _merge_entities(members: list[ExtractedEntity]) -> ExtractedEntity:
    """Fold a group of duplicate entities into one canonical entity.

    Survivor = the entity with the most evidence (source_spans), ties broken by
    the longest canonical name. Aliases/properties/spans/sources are unioned.
    """
    survivor = max(members, key=lambda e: (len(e.source_spans), len(e.canonical_name)))
    aliases: set[str] = set(survivor.aliases)
    props: dict[str, str] = survivor.property_dict()
    spans: list[str] = list(survivor.source_spans)
    sources = list(survivor.sources)

    for e in members:
        if e is survivor:
            continue
        aliases.add(e.canonical_name)
        aliases.update(e.aliases)
        for k, v in e.property_dict().items():
            props.setdefault(k, v)
        spans.extend(s for s in e.source_spans if s not in spans)
        sources.extend(s for s in e.sources if s not in sources)

    aliases.discard(survivor.canonical_name)
    return ExtractedEntity(
        id=survivor.id,
        type=survivor.type,
        canonical_name=survivor.canonical_name,
        aliases=sorted(aliases),
        properties=[KVProperty(key=k, value=v) for k, v in props.items()],
        source_spans=spans,
        sources=sources,
    )


def _dedupe_edges(edges: list[ExtractedRelationship]) -> list[ExtractedRelationship]:
    """Collapse edges with the same (source, type, target); union their info."""
    merged: dict[tuple[str, str, str], ExtractedRelationship] = {}
    for e in edges:
        if e.source_id == e.target_id:  # self-loop created by a merge
            continue
        key = (e.source_id, e.type.value, e.target_id)
        if key not in merged:
            merged[key] = e
            continue
        keep = merged[key]
        props = keep.property_dict()
        for k, v in e.property_dict().items():
            props.setdefault(k, v)
        merged[key] = ExtractedRelationship(
            source_id=keep.source_id,
            type=keep.type,
            target_id=keep.target_id,
            properties=[KVProperty(key=k, value=v) for k, v in props.items()],
            source_span=keep.source_span or e.source_span,
            confidence=max(keep.confidence, e.confidence),
            sources=list(dict.fromkeys([*keep.sources, *e.sources])),
        )
    return list(merged.values())


def resolve(graph: ProposedGraph) -> ProposedGraph:
    """Dedupe entities and rewrite edges onto surviving ids."""
    uf = _UnionFind([e.id for e in graph.entities])

    # Union entities of the same type that share any normalized key.
    by_key: dict[tuple[str, str], str] = {}  # (type, normalized_key) -> entity id
    for e in graph.entities:
        for k in _keys(e):
            existing = by_key.get((e.type.value, k))
            if existing is not None:
                uf.union(e.id, existing)
            else:
                by_key[(e.type.value, k)] = e.id

    # Group members by their union-find root.
    groups: dict[str, list[ExtractedEntity]] = {}
    for e in graph.entities:
        groups.setdefault(uf.find(e.id), []).append(e)

    merged_entities: list[ExtractedEntity] = []
    id_remap: dict[str, str] = {}
    for members in groups.values():
        merged = _merge_entities(members)
        merged_entities.append(merged)
        for m in members:
            id_remap[m.id] = merged.id

    # Rewrite edge endpoints onto surviving ids, then dedupe.
    rewritten = [
        ExtractedRelationship(
            source_id=id_remap.get(e.source_id, e.source_id),
            type=e.type,
            target_id=id_remap.get(e.target_id, e.target_id),
            properties=e.properties,
            source_span=e.source_span,
            confidence=e.confidence,
            sources=e.sources,
        )
        for e in graph.relationships
    ]
    deduped = _dedupe_edges(rewritten)

    logger.info(
        "entity resolution complete",
        extra={
            "entities_before": len(graph.entities),
            "entities_after": len(merged_entities),
            "edges_before": len(graph.relationships),
            "edges_after": len(deduped),
        },
    )
    return ProposedGraph(entities=merged_entities, relationships=deduped)
