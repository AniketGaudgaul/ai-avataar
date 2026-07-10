"""Section-aware, hierarchical chunking of parsed markdown (spec 5.4).

The rules, in order:

1. Split at heading boundaries first — each heading starts a new section; a
   section owns the content blocks between it and the next heading. Never split
   mid-section arbitrarily.
2. If a section exceeds the child-token target, split it at *paragraph* (block)
   boundaries into child chunks — never below a paragraph, so metric sentences
   ("cut cost 70% by X") always stay whole (rule 4). Each child records the
   section's `heading_path` breadcrumb (prepended at embed time, not baked in).
3. Never split an atomic block (code / table / figure) — each becomes its own
   child even if it exceeds the target.
5. Merge tiny sections (< tiny-merge tokens) upward into the previous section
   instead of emitting orphans.
7. Small-to-big — every child carries its `parent_section_id`; the full parent
   section is kept separately (`ParentSection`) for answer-time expansion.

Reference/bibliography sections are dropped: 40+ citation lines are retrieval
noise for a career Q&A assistant.

The contextual-retrieval prefix (rule 6) is deliberately NOT applied here — it is
a Gemini call and belongs to the embed stage; this module stays LLM-free so chunk
quality can be reviewed before any quota is spent.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from app.config import settings
from app.core.logging import get_logger
from app.ingestion.vector.schema import (
    Chunk,
    ChunkedDoc,
    ChunkMetadata,
    ContentType,
    ParentSection,
    ParsedDoc,
)
from app.ingestion.vector.tokens import count_tokens

logger = get_logger(__name__)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_IMAGE_RE = re.compile(r"^!\[([^\]]*)\]\([^)]*\)\s*$")
# A bare `---` / `***` / `___` rule carries no meaning and would otherwise become
# its own one-token chunk.
_HRULE_RE = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$")
_DROP_SECTION_RE = re.compile(
    r"^(references|bibliography|acknowledge?ments?|(table of )?contents)\b", re.IGNORECASE
)
# Metric-ish content worth tagging (percentages, multipliers, big counts, common
# ML scores) — these are the highest-value explanatory-query targets.
_METRIC_RE = re.compile(
    r"\d[\d,\.]*\s?%|\b\d+(?:\.\d+)?\s?[x×]\b|\b\d{1,3}(?:,\d{3})+\b|\b(?:F1|ROUGE|BLEU|BERTScore)\b",
    re.IGNORECASE,
)
_BREADCRUMB = " ▸ "


# --- Markdown block parsing -------------------------------------------------

@dataclass
class _Block:
    kind: str            # "heading" | "para" | "code" | "table" | "figure"
    text: str            # raw markdown (a figure keeps its `![alt](uri)` form)
    level: int = 0       # heading level, else 0
    alt: str = ""        # figure only: the alt text, used as the chunk's content

    @property
    def atomic(self) -> bool:
        return self.kind in ("code", "table", "figure")

    @property
    def content(self) -> str:
        """What this block contributes to a chunk. A figure contributes its alt
        text, not `![alt](./images/x.png)` — the uri is path noise in an embedding
        and the raw form is only kept so the image linker can find the reference."""
        return self.alt if self.kind == "figure" and self.alt else self.text


def _parse_blocks(markdown: str) -> list[_Block]:
    """Break markdown into a flat, ordered list of blocks. Fenced code, pipe
    tables, and standalone images are each captured as atomic blocks."""
    blocks: list[_Block] = []
    lines = markdown.splitlines()
    i, n = 0, len(lines)
    para: list[str] = []

    def flush_para() -> None:
        if para:
            text = "\n".join(para).strip()
            if text:
                blocks.append(_Block("para", text))
            para.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Fenced code block — consume to closing fence.
        if stripped.startswith("```"):
            flush_para()
            buf = [line]
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            if i < n:
                buf.append(lines[i])  # closing fence
                i += 1
            blocks.append(_Block("code", "\n".join(buf)))
            continue

        # Heading.
        m = _HEADING_RE.match(line)
        if m:
            flush_para()
            blocks.append(_Block("heading", m.group(2).strip(), level=len(m.group(1))))
            i += 1
            continue

        # Pipe table — consecutive lines beginning with "|".
        if stripped.startswith("|"):
            flush_para()
            buf = []
            while i < n and lines[i].strip().startswith("|"):
                buf.append(lines[i])
                i += 1
            blocks.append(_Block("table", "\n".join(buf)))
            continue

        # Standalone image / figure.
        if m := _IMAGE_RE.match(stripped):
            flush_para()
            blocks.append(_Block("figure", stripped, alt=m.group(1).strip()))
            i += 1
            continue

        # Horizontal rule — a separator, not content.
        if _HRULE_RE.match(stripped):
            flush_para()
            i += 1
            continue

        # Blank line ends a paragraph.
        if not stripped:
            flush_para()
            i += 1
            continue

        para.append(line)
        i += 1

    flush_para()
    return blocks


# --- Section tree -----------------------------------------------------------

@dataclass
class _Section:
    heading_path: str
    section: str
    blocks: list[_Block] = field(default_factory=list)

    def text(self) -> str:
        """Raw section text — keeps `![alt](uri)` intact so `images.link_images`
        can resolve each figure to this section. Use `content()` for anything
        that gets embedded or shown."""
        return "\n\n".join(b.text for b in self.blocks).strip()

    def content(self) -> str:
        return "\n\n".join(b.content for b in self.blocks).strip()


def _build_sections(blocks: list[_Block], doc_title: str) -> list[_Section]:
    """Group blocks into sections by heading, tracking the breadcrumb stack.
    Content before the first heading becomes a synthetic preamble section."""
    sections: list[_Section] = []
    stack: list[tuple[int, str]] = []  # (level, title)
    current: _Section | None = None

    def breadcrumb() -> str:
        return _BREADCRUMB.join(title for _, title in stack)

    for block in blocks:
        if block.kind == "heading":
            # Pop siblings/deeper, then push this heading.
            while stack and stack[-1][0] >= block.level:
                stack.pop()
            stack.append((block.level, block.text))
            current = _Section(heading_path=breadcrumb(), section=block.text)
            sections.append(current)
        else:
            if current is None:
                current = _Section(
                    heading_path=doc_title or "Preamble",
                    section=doc_title or "Preamble",
                )
                sections.append(current)
            current.blocks.append(block)

    # Drop empty and reference/bibliography sections.
    kept = [
        s for s in sections
        if s.blocks and not _DROP_SECTION_RE.match(s.section.strip())
    ]
    return _merge_tiny(kept)


def _is_descendant(child_path: str, ancestor_path: str) -> bool:
    """True if `child_path`'s breadcrumb is nested under `ancestor_path`."""
    return child_path.startswith(ancestor_path + _BREADCRUMB)


def _merge_tiny(sections: list[_Section]) -> list[_Section]:
    """Merge sections smaller than the tiny-merge threshold into the previous
    kept section (spec 5.4 rule 5), but ONLY when that previous section is a true
    ancestor — i.e. a tiny subsection folds *into its own parent section*. A short
    but distinct section (e.g. Conclusion following Introduction) is never merged
    into an unrelated sibling; it is kept as its own small chunk. This trades the
    occasional small orphan for guaranteed-correct heading provenance."""
    threshold = settings.chunk_tiny_merge_tokens
    out: list[_Section] = []
    for s in sections:
        if (
            out
            and count_tokens(s.text()) < threshold
            and _is_descendant(s.heading_path, out[-1].heading_path)
        ):
            out[-1].blocks.extend(s.blocks)
        else:
            out.append(s)
    return out


# --- Chunk assembly ---------------------------------------------------------

def _classify(child_blocks: list[_Block]) -> ContentType:
    kinds = {b.kind for b in child_blocks}
    if kinds == {"figure"}:
        return ContentType.DIAGRAM_CAPTION
    if "code" in kinds:
        return ContentType.CODE
    if "table" in kinds:
        return ContentType.TABLE
    text = "\n".join(b.content for b in child_blocks)
    return ContentType.METRIC if _METRIC_RE.search(text) else ContentType.PROSE


def _split_section(section: _Section, max_tokens: int) -> list[list[_Block]]:
    """Pack a section's blocks into children under `max_tokens`. Atomic blocks
    are flushed to their own child; a single oversize paragraph stays whole."""
    children: list[list[_Block]] = []
    buf: list[_Block] = []
    buf_tokens = 0
    lead_in_max = settings.chunk_tiny_merge_tokens

    def flush() -> None:
        nonlocal buf, buf_tokens
        if buf:
            children.append(buf)
            buf, buf_tokens = [], 0

    for block in section.blocks:
        if block.atomic:
            # A short lead-in ("Every chunk carries a metadata structure of the
            # shape:") is meaningless alone and is exactly the sentence that says
            # what the table/code below it *is* — keep it attached rather than
            # emitting it as an orphan child.
            if buf and buf_tokens <= lead_in_max:
                children.append([*buf, block])
                buf, buf_tokens = [], 0
                continue
            flush()
            children.append([block])
            continue
        btok = count_tokens(block.text)
        if buf and buf_tokens + btok > max_tokens:
            flush()
        buf.append(block)
        buf_tokens += btok
        if buf_tokens >= max_tokens:  # single oversize paragraph → its own child
            flush()
    flush()
    return children or [[]]


def parent_section_id(doc_id: str, section_index: int) -> str:
    """The canonical parent id for the n-th section of a document.

    Shared with the image linker (`images.py`) so an image can point at exactly
    the section id the chunker will emit — the two passes must agree."""
    return f"{doc_id}:s{section_index:03d}"


def section_index(doc: ParsedDoc) -> list[ParentSection]:
    """The document's sections, in the same order (and with the same ids) that
    `chunk_parsed_doc` will produce. Lets the image-linking pass resolve a
    caption's surrounding heading without re-running chunking."""
    sections = _build_sections(_parse_blocks(doc.markdown), doc.title)
    return [
        ParentSection(
            parent_section_id=parent_section_id(doc.doc_id, i),
            doc_id=doc.doc_id,
            heading_path=s.heading_path,
            section=s.section,
            text=s.text(),
            token_count=count_tokens(s.text()),
        )
        for i, s in enumerate(sections)
    ]


def chunk_parsed_doc(doc: ParsedDoc) -> ChunkedDoc:
    """Turn a parsed document into section-aware child chunks + parent sections."""
    child_max = settings.chunk_child_max_tokens
    blocks = _parse_blocks(doc.markdown)
    sections = _build_sections(blocks, doc.title)

    now = datetime.now(UTC).isoformat()
    citation_base = doc.title or doc.doc_id
    chunks: list[Chunk] = []
    parents: list[ParentSection] = []
    idx = 0

    for s_i, section in enumerate(sections):
        section_text = section.content()
        parent_id = parent_section_id(doc.doc_id, s_i)
        parents.append(
            ParentSection(
                parent_section_id=parent_id,
                doc_id=doc.doc_id,
                heading_path=section.heading_path,
                section=section.section,
                text=section_text,
                token_count=count_tokens(section_text),
            )
        )

        for child_blocks in _split_section(section, child_max):
            if not child_blocks:
                continue
            text = "\n\n".join(b.content for b in child_blocks).strip()
            if not text:
                continue
            meta = ChunkMetadata(
                chunk_id=f"{doc.doc_id}:c{idx:04d}",
                doc_id=doc.doc_id,
                source_type=doc.source_type,
                heading_path=section.heading_path,
                section=section.section,
                chunk_index=idx,
                parent_section_id=parent_id,
                content_type=_classify(child_blocks),
                citation_label=f"{citation_base} — {section.section}",
                content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                ingested_at=now,
                token_count=count_tokens(text),
            )
            chunks.append(Chunk(text=text, metadata=meta))
            idx += 1

    logger.info(
        "chunking complete",
        extra={
            "doc_id": doc.doc_id,
            "n_sections": len(sections),
            "n_parents": len(parents),
            "n_chunks": len(chunks),
        },
    )
    return ChunkedDoc(
        doc_id=doc.doc_id,
        source_type=doc.source_type,
        source_path=doc.source_path,
        title=doc.title,
        parser=doc.parser,
        parsed_at=doc.parsed_at,
        chunks=chunks,
        parent_sections=parents,
        images=doc.images,
    )
