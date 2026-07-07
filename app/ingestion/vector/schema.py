"""Vector-path data models: parsed documents, images, chunks, and chunk metadata.

Mirrors the chunk-metadata schema in spec 5.5. Two layers:

1. **Parse output** (`ParsedDoc`, `ParsedImage`) — what the LlamaParse v2 stage
   produces: the document as markdown plus any figure/table images it saved
   (kept for Tier-3 multimodal embedding; not chunked here).
2. **Chunk models** (`Chunk`, `ChunkMetadata`, `ParentSection`) — the section-aware
   chunks (spec 5.4) and the full metadata each carries.

`ChunkMetadata.text` is stored raw for display/citation. The contextual-retrieval
prefix (spec 5.4 step 6) and the `heading_path` breadcrumb are prepended only at
*embed* time, not baked into the stored chunk text — so this stage stays free of
any LLM call and the reviewer sees exactly what was extracted.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from app.ingestion.graph.schema import SourceType  # reuse the shared provenance enum


class ContentType(StrEnum):
    """What a chunk mostly contains (spec 5.5 `content_type`) — drives filtering
    and weighting, and marks atomic blocks that must never be split."""

    PROSE = "prose"
    CODE = "code"
    TABLE = "table"
    METRIC = "metric"
    DIAGRAM_CAPTION = "diagram_caption"


class Modality(StrEnum):
    TEXT = "text"
    IMAGE = "image"


class ParsedImage(BaseModel):
    """A figure/table image LlamaParse saved out of the document.

    Kept alongside the markdown so the Tier-3 multimodal path can embed these
    directly (Gemini Embedding 2) without re-parsing. `category` is LlamaParse's
    own class: 'layout' (cropped figure/table region), 'embedded' (raster image
    embedded in the PDF), or 'screenshot' (full page)."""

    filename: str
    category: str | None = None          # embedded | layout | screenshot
    page_number: int | None = None
    local_path: str | None = None        # where we saved the bytes (if downloaded)
    caption: str = ""
    content_type: str | None = None      # MIME, e.g. image/png
    size_bytes: int | None = None


class ParsedDoc(BaseModel):
    """The output of the parse stage for one source document."""

    doc_id: str
    source_type: SourceType
    source_path: str
    title: str = ""                      # first H1 in the markdown, if any
    markdown: str                        # whole-document markdown (chunk input)
    images: list[ParsedImage] = Field(default_factory=list)
    parser: str = ""                     # e.g. "llamaparse-v2:agentic_plus"
    parsed_at: str = ""


class ChunkMetadata(BaseModel):
    """Per-chunk metadata (spec 5.5).

    `linked_entities` and `project_tag` bridge to the Neo4j graph; both are left
    empty/None at chunk time and filled by a later entity-linking pass (a paper
    links to a `Publication` node, not a `Project`, so `project_tag` stays None)."""

    chunk_id: str
    doc_id: str
    source_type: SourceType
    heading_path: str                    # breadcrumb, e.g. "MedSumm ▸ Method ▸ Retriever"
    section: str                         # leaf heading title
    chunk_index: int
    parent_section_id: str               # small-to-big: full parent section id
    project_tag: str | None = None       # must match a Neo4j Project id (None for papers)
    linked_entities: list[str] = Field(default_factory=list)
    content_type: ContentType = ContentType.PROSE
    modality: Modality = Modality.TEXT
    image_uri: str | None = None
    citation_label: str = ""             # human-readable source for UI + guardrail
    date: str | None = None
    date_range: str | None = None
    content_hash: str = ""
    version: int = 1
    ingested_at: str = ""
    token_count: int = 0


class Chunk(BaseModel):
    """A retrievable child chunk: raw text plus its metadata."""

    text: str
    metadata: ChunkMetadata


class ParentSection(BaseModel):
    """A full section, kept for small-to-big retrieval — children embed for
    precision, but at answer time the generator can be handed the whole parent."""

    parent_section_id: str
    doc_id: str
    heading_path: str
    section: str
    text: str
    token_count: int


class ChunkedDoc(BaseModel):
    """Everything the chunking stage produces for one document, ready for the
    review gate (and, later, embedding)."""

    doc_id: str
    source_type: SourceType
    source_path: str
    title: str = ""
    parser: str = ""
    parsed_at: str = ""
    chunks: list[Chunk] = Field(default_factory=list)
    parent_sections: list[ParentSection] = Field(default_factory=list)
    images: list[ParsedImage] = Field(default_factory=list)
