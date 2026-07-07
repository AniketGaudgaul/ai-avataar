"""Knowledge-graph schema: node/relationship types and the extraction models.

Mirrors spec 5.6. Two roles:

1. **Neo4j schema** — the 8 allowed node labels (`NodeType`), the allowed
   relationship types (`RelationType`), and which (source-type -> target-type)
   pairs each relationship may connect (`RELATION_SCHEMA`). This constrains the
   LLM to a fixed ontology so it can't invent node/edge kinds.
2. **Extraction wire models** — the JSON shapes Gemini returns for the entity
   pass and relationship pass (`ExtractedEntity`, `ExtractedRelationship`),
   plus `ProposedGraph` bundling a full extraction with provenance.

Gemini structured output does not support free-form maps, so entity/edge
`properties` are carried as a list of `KVProperty(key, value)` pairs and
flattened to a dict at load time.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class NodeType(StrEnum):
    """The 8 allowed node labels (spec 5.6). No other types may be created."""

    PERSON = "Person"
    COMPANY = "Company"
    ROLE = "Role"
    PROJECT = "Project"
    SKILL = "Skill"
    TECHNOLOGY = "Technology"
    PUBLICATION = "Publication"
    ACHIEVEMENT = "Achievement"


class RelationType(StrEnum):
    """The allowed relationship types (spec 5.6)."""

    WORKED_AT = "WORKED_AT"      # (Person)->(Company) {start, end}
    HELD_ROLE = "HELD_ROLE"      # (Person)->(Role)
    AT = "AT"                    # (Role)->(Company)
    LED = "LED"                  # (Person)->(Project)
    USED = "USED"                # (Project)->(Technology)
    DEMONSTRATES = "DEMONSTRATES"  # (Project)->(Skill)
    PUBLISHED = "PUBLISHED"      # (Person)->(Publication)
    WON = "WON"                  # (Person)->(Achievement)
    PART_OF = "PART_OF"          # (Project)->(Company)


# Allowed (source node type, target node type) endpoints for each relationship.
# Fed to the relationship-extraction prompt and used to drop invalid edges.
RELATION_SCHEMA: dict[RelationType, tuple[NodeType, NodeType]] = {
    RelationType.WORKED_AT: (NodeType.PERSON, NodeType.COMPANY),
    RelationType.HELD_ROLE: (NodeType.PERSON, NodeType.ROLE),
    RelationType.AT: (NodeType.ROLE, NodeType.COMPANY),
    RelationType.LED: (NodeType.PERSON, NodeType.PROJECT),
    RelationType.USED: (NodeType.PROJECT, NodeType.TECHNOLOGY),
    RelationType.DEMONSTRATES: (NodeType.PROJECT, NodeType.SKILL),
    RelationType.PUBLISHED: (NodeType.PERSON, NodeType.PUBLICATION),
    RelationType.WON: (NodeType.PERSON, NodeType.ACHIEVEMENT),
    RelationType.PART_OF: (NodeType.PROJECT, NodeType.COMPANY),
}


class SourceType(StrEnum):
    """Provenance tag: which source document a fact came from (spec 5.5)."""

    RESUME = "resume"
    NARRATIVE = "narrative"
    PROJECT = "project"
    PAPER = "paper"
    ACHIEVEMENTS = "achievements"
    HOW_I_BUILT_THIS = "how_i_built_this"


class KVProperty(BaseModel):
    """A single node/edge property. (List-of-pairs because Gemini structured
    output can't emit free-form object maps.)"""

    key: str
    value: str


class ExtractedEntity(BaseModel):
    """A canonical entity from the entity pass (spec 5.3 step 2)."""

    id: str = Field(description="Stable lowercase slug, e.g. 'yarnit-innovations'.")
    type: NodeType
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    properties: list[KVProperty] = Field(default_factory=list)
    source_spans: list[str] = Field(
        default_factory=list, description="Verbatim snippets the entity was drawn from."
    )
    sources: list[SourceType] = Field(
        default_factory=list, description="Which marked source block(s) it appeared in."
    )

    def property_dict(self) -> dict[str, str]:
        return {p.key: p.value for p in self.properties}


class ExtractedRelationship(BaseModel):
    """A typed edge from the relationship pass (spec 5.3 step 3)."""

    source_id: str = Field(description="`id` of an entity from the entity pass.")
    type: RelationType
    target_id: str = Field(description="`id` of an entity from the entity pass.")
    properties: list[KVProperty] = Field(
        default_factory=list, description="e.g. start/end dates for WORKED_AT."
    )
    source_span: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    sources: list[SourceType] = Field(default_factory=list)

    def property_dict(self) -> dict[str, str]:
        return {p.key: p.value for p in self.properties}


class ProposedGraph(BaseModel):
    """A full extraction ready for the manual-review gate (spec 5.3 step 5)."""

    entities: list[ExtractedEntity] = Field(default_factory=list)
    relationships: list[ExtractedRelationship] = Field(default_factory=list)

    def entity_by_id(self) -> dict[str, ExtractedEntity]:
        return {e.id: e for e in self.entities}
