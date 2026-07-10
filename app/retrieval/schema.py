"""Retrieval result models."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RetrievedContext(BaseModel):
    """A parent section surfaced by retrieval (small-to-big): the matched child
    chunks are collapsed into their full parent section for generation context,
    while keeping the citation label + which children actually matched."""

    parent_section_id: str
    doc_id: str
    source_type: str
    heading_path: str
    citation_label: str
    text: str                       # full parent section (children stitched in order)
    score: float                    # best RRF score among matched children
    matched_chunk_ids: list[str] = Field(default_factory=list)
    content_types: list[str] = Field(default_factory=list)


class RetrievedImage(BaseModel):
    """A diagram/figure surfaced by the image pass (spec 5.7, 6.3).

    Returned separately from `RetrievedContext` rather than merged into it: image
    and text scores come from different bands of the embedding space, so a single
    ranked list is meaningless. `caption` is the text sidecar that was embedded
    alongside the pixels; `linked_section_id` points back at the prose section the
    figure illustrates."""

    chunk_id: str
    image_uri: str
    doc_id: str
    source_type: str
    heading_path: str
    citation_label: str
    caption: str                    # the text sidecar
    score: float
    linked_section_id: str | None = None


class GraphFact(BaseModel):
    """A citable relationship fact from the knowledge graph."""

    subject: str
    relation: str
    object: str
    properties: dict[str, str] = Field(default_factory=dict)  # e.g. {start, end}
    source_docs: list[str] = Field(default_factory=list)

    def as_sentence(self) -> str:
        prop = ""
        if self.properties:
            prop = " (" + ", ".join(f"{k}: {v}" for k, v in self.properties.items()) + ")"
        return f"{self.subject} —{self.relation}→ {self.object}{prop}"
