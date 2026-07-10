"""Image → section linking and image-chunk construction (spec 5.7, Tier 3).

An image is only useful to retrieval if it knows *where it came from*. This
module turns the flat `ParsedDoc.images` list into retrievable `Chunk`s with
`modality=image`, each carrying a **text sidecar**: the figure caption plus the
heading breadcrumb of the section it illustrates.

Why a sidecar at all, given Gemini Embedding 2 is natively multimodal?

1. **BM25 needs tokens.** An image has none, so without a sidecar it is invisible
   to the sparse half of hybrid retrieval (spec 5.7).
2. **The modality gap is real and it buries images.** Measured on this corpus:
   for the query "show me the MedSumm architecture diagram", the correct diagram
   embedded *bare* ranks 21st of 22 against the paper's own text chunks
   (cos 0.379 vs 0.71-0.77 for prose). Text-query↔image cosines simply live in a
   lower band than text↔text ones, so the two are not score-comparable.
   Embedding the image **fused with its sidecar** in one call lifts it to
   cos 0.639, and lifts caption-query top-1 accuracy from 2/4 to 4/4.

So each image chunk is embedded once, as `[sidecar_text, image_bytes]` in a
single `embed_content` call → one vector in the same space (see
`embedder.embed_image`). The sidecar text is *also* what feeds BM25 and what the
UI shows as the caption. Retrieval still queries images in their own
modality-filtered pass (see `retrieval/vector.py`), because fusing narrows the
gap but does not close it.

Linking strategies, in priority order:

- **`inline`** — the markdown contains `![caption](uri)` and the uri's basename
  matches the saved image. Exact, free, no LLM. This is the path authored project
  docs take (spec 5.1 S2), and the one that matters for new content.
- **`caption_match`** — for PDFs parsed to a flat image list (no inline refs),
  ask a vision model which of the document's `Figure N:` / `Table N:` caption
  lines belongs to each image. Opt-in, one cheap call per image, and it writes a
  reviewable artifact — the same extract-then-approve discipline as the graph
  path (spec 5.3 step 6). Needed because positional order is *not* a reliable
  signal: in the ECIR paper, `page_7_table_1` is Figure 2 while
  `page_7_chart_1` is Figure 3, so sorting by filename mislabels diagrams.
- **`override`** — a reviewed `image_links.json` (`filename → "Figure 4"`, or `""`
  to force unlinked) always wins. This is the manual gate: caption matching is a
  model judgement on genuinely ambiguous inputs, so it must be correctable.
- **`unlinked`** — no caption found, or the match was below the confidence floor;
  the sidecar falls back to the document title. Still retrievable, just weaker.

Note that captions are **not** claimed exclusively. A parser routinely emits two
overlapping crops of one figure (here `page_11_table_2` is Figure 5 whole and
`page_11_table_3` is its table alone), so both must be allowed to resolve to
`Figure 5`. An earlier exclusive-assignment version starved the true Figure 2 of
its caption because a different image matched it first.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings
from app.core.assets import mime_for, store_image
from app.core.gemini import _is_retryable
from app.core.logging import get_logger
from app.ingestion.vector.chunker import section_index
from app.ingestion.vector.schema import (
    Chunk,
    ChunkMetadata,
    ContentType,
    Modality,
    ParentSection,
    ParsedDoc,
    ParsedImage,
)
from app.ingestion.vector.tokens import count_tokens

logger = get_logger(__name__)

# "Figure 4: Model structure of ...", "Table 1. Performance of ...", "Fig -1: ..."
_CAPTION_RE = re.compile(
    r"^\**\s*(figure|fig\.?|table|algorithm)\s*[-–—]?\s*(\d+)\s*[:.]\s*(.+?)\s*\**$",
    re.IGNORECASE,
)
_INLINE_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)")


class Caption(BaseModel):
    """A `Figure N: ...` line found in the markdown, with the section it sits in."""

    label: str                  # normalised, e.g. "Figure 4"
    text: str                   # caption prose (no label prefix)
    heading_path: str
    section: str
    parent_section_id: str

    @property
    def full(self) -> str:
        return f"{self.label}: {self.text}"


class _CaptionChoice(BaseModel):
    """Vision model's answer when matching one image to a candidate caption."""

    caption_index: int = Field(description="0-based index into the candidates, or -1 if none match")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


# --- Extraction -------------------------------------------------------------

def _normalise_label(kind: str, number: str) -> str:
    k = kind.lower().rstrip(".")
    canonical = {"fig": "Figure", "figure": "Figure", "table": "Table", "algorithm": "Algorithm"}
    return f"{canonical.get(k, kind.title())} {number}"


def _strip_emphasis(text: str) -> str:
    """Drop markdown bold/italic markers — they are noise in a BM25 sidecar."""
    return re.sub(r"[*_]{1,3}(?=\S)|(?<=\S)[*_]{1,3}", "", text).strip()


def extract_captions(doc: ParsedDoc, sections: list[ParentSection]) -> list[Caption]:
    """Find every figure/table caption line, tagged with its enclosing section."""
    captions: list[Caption] = []
    for sec in sections:
        for line in sec.text.splitlines():
            m = _CAPTION_RE.match(line.strip())
            if not m:
                continue
            captions.append(
                Caption(
                    label=_normalise_label(m.group(1), m.group(2)),
                    text=_strip_emphasis(m.group(3)),
                    heading_path=sec.heading_path,
                    section=sec.section,
                    parent_section_id=sec.parent_section_id,
                )
            )
    logger.info("captions extracted", extra={"doc_id": doc.doc_id, "n": len(captions)})
    return captions


def _inline_refs(sections: list[ParentSection]) -> dict[str, tuple[str, ParentSection]]:
    """Map image basename → (alt text, section) for every inline `![alt](uri)`."""
    refs: dict[str, tuple[str, ParentSection]] = {}
    for sec in sections:
        for alt, uri in _INLINE_IMG_RE.findall(sec.text):
            refs[Path(uri).name] = (alt.strip(), sec)
    return refs


def eligible_images(doc: ParsedDoc) -> list[ParsedImage]:
    """Images worth embedding: real figure/table crops that exist on disk.

    The category/size filters exist to reject *parser* noise — LlamaParse's
    `embedded` rasters are usually sub-images cropped out of a larger figure (the
    symptom thumbnails inside Figure 1), and tiny crops are decorative. They must
    not apply to `category="inline"` images: an author who wrote `![...](x.svg)`
    into their doc meant it, and a 5 KB rasterised SVG is a full architecture
    diagram, not a thumbnail."""
    allowed = settings.ingest_image_categories_list
    kept: list[ParsedImage] = []
    for img in doc.images:
        if not img.local_path or not Path(img.local_path).exists():
            continue
        if img.category != "inline":
            if allowed and (img.category or "") not in allowed:
                continue
            size = img.size_bytes or Path(img.local_path).stat().st_size
            if size < settings.ingest_image_min_bytes:
                continue
        kept.append(img)
    logger.info(
        "images filtered",
        extra={"doc_id": doc.doc_id, "total": len(doc.images), "eligible": len(kept)},
    )
    return kept


# --- Linking ----------------------------------------------------------------

@retry(
    retry=retry_if_exception(_is_retryable),
    wait=wait_exponential(multiplier=1, min=15, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)
def _vision_choice(image_bytes: bytes, mime: str, prompt: str, model: str) -> _CaptionChoice | None:
    """One vision call, retrying free-tier 429s and transient 503s like the rest
    of the Gemini surface. Without this a single 503 silently leaves a figure
    uncaptioned, which is a retrieval regression that is easy to miss."""
    from google.genai import types

    from app.core.gemini import get_client

    resp = get_client().models.generate_content(
        model=model,
        contents=[
            types.Part.from_bytes(data=image_bytes, mime_type=mime),
            types.Part.from_text(text=prompt),
        ],
        config=types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=_CaptionChoice,
        ),
    )
    return resp.parsed


def _match_caption_with_vision(
    img: ParsedImage, captions: list[Caption], model: str
) -> tuple[int, float, bool]:
    """Ask a vision model which caption describes this image.

    Returns `(index_into_captions, confidence, api_failed)`. A persistent API
    failure degrades to -1 (unlinked, still retrievable via its fallback sidecar)
    rather than aborting the ingest, but is flagged so the caller can stop calling
    a model that is refusing every request."""
    listing = "\n".join(f"{i}. {c.full}" for i, c in enumerate(captions))
    page = f" It was extracted from page {img.page_number}." if img.page_number else ""
    prompt = (
        "You are matching a figure extracted from an academic paper to its caption.\n"
        f"Below is the image, then the numbered captions found in the paper.{page}\n"
        "Reply with the index of the caption that describes THIS image, or -1 if none do.\n"
        "Judge by what the image actually depicts (a taxonomy tree, an architecture "
        "diagram, a results table, a sample-data table), not by caption order.\n"
        "The image may be a partial crop of a larger figure — match it to that figure.\n"
        "Set confidence below 0.5 if several captions plausibly fit.\n\n"
        f"Captions:\n{listing}"
    )
    path = Path(img.local_path or "")
    try:
        choice = _vision_choice(path.read_bytes(), mime_for(path), prompt, model)
    except Exception as exc:  # noqa: BLE001 — one bad image must not fail the doc
        logger.warning("caption match failed", extra={"image": img.filename, "error": str(exc)})
        return -1, 0.0, True
    if choice is None or not (0 <= choice.caption_index < len(captions)):
        return -1, 0.0, False
    return choice.caption_index, choice.confidence, False


def _apply_caption(img: ParsedImage, cap: Caption, method: str) -> None:
    img.caption, img.figure_label = cap.text, cap.label
    img.heading_path, img.section = cap.heading_path, cap.section
    img.linked_section_id = cap.parent_section_id
    img.link_method = method


def link_images(
    doc: ParsedDoc,
    *,
    use_vision: bool = False,
    model: str | None = None,
    overrides: dict[str, str] | None = None,
) -> list[ParsedImage]:
    """Attach caption + section to each eligible image, returning linked copies.

    `overrides` maps an image filename to a figure label ("Figure 4") that wins
    over any inferred match, or to "" to force it unlinked."""
    sections = section_index(doc)
    captions = extract_captions(doc, sections)
    inline = _inline_refs(sections)
    model = model or settings.image_caption_match_model
    overrides = overrides or {}

    by_label = {c.label.lower(): c for c in captions}
    linked: list[ParsedImage] = []
    # A free-tier daily quota looks like a 429, which `_is_retryable` treats as the
    # per-minute throttle and backs off on — so an exhausted quota would otherwise
    # burn 5 slow retries per image and silently unlink the lot. Stop calling after
    # two consecutive hard failures and let the reviewed overrides carry the doc.
    consecutive_failures = 0

    for img in eligible_images(doc):
        out = img.model_copy(deep=True)
        out.link_method = "unlinked"

        # 0. Reviewed override wins outright.
        if img.filename in overrides:
            label = overrides[img.filename].strip()
            cap = by_label.get(label.lower())
            if cap:
                _apply_caption(out, cap, "override")
            elif label:
                logger.warning(
                    "override names an unknown caption",
                    extra={"image": img.filename, "label": label},
                )

        # 1. Exact: the markdown references this file inline (authored docs).
        elif img.filename in inline:
            alt, sec = inline[img.filename]
            cap = by_label.get(alt.lower().rstrip(":").strip())
            out.caption = cap.text if cap else alt
            out.figure_label = cap.label if cap else ""
            out.heading_path, out.section = sec.heading_path, sec.section
            out.linked_section_id = sec.parent_section_id
            out.link_method = "inline"

        # 2. Vision-assisted caption match (opt-in) for flat PDF image lists.
        # Captions are not consumed: overlapping crops of one figure share it.
        elif use_vision and captions and consecutive_failures < 2:
            idx, confidence, failed = _match_caption_with_vision(out, captions, model)
            consecutive_failures = consecutive_failures + 1 if failed else 0
            out.link_confidence = confidence
            if idx >= 0 and confidence >= settings.image_caption_min_confidence:
                _apply_caption(out, captions[idx], "caption_match")
            elif idx >= 0:
                logger.info(
                    "caption match below confidence floor; leaving unlinked",
                    extra={"image": img.filename, "confidence": confidence},
                )

        linked.append(out)

    if use_vision and consecutive_failures >= 2:
        logger.error(
            "caption matching abandoned after repeated API failures (quota exhausted?); "
            "remaining images left unlinked — supply a reviewed image_links.json instead",
        )

    n_linked = sum(1 for i in linked if i.link_method != "unlinked")
    logger.info(
        "images linked",
        extra={"doc_id": doc.doc_id, "linked": n_linked, "unlinked": len(linked) - n_linked},
    )
    return linked


# --- Chunk construction -----------------------------------------------------

def sidecar_text(doc: ParsedDoc, img: ParsedImage) -> str:
    """The image's text representation: caption + heading breadcrumb (spec 5.7).

    Fed to BM25, fused into the dense embedding, and shown as the UI caption."""
    parts: list[str] = []
    if img.heading_path:
        parts.append(img.heading_path)
    elif doc.title:
        parts.append(doc.title)
    if img.figure_label and img.caption:
        parts.append(f"{img.figure_label}: {img.caption}")
    elif img.caption:
        parts.append(img.caption)
    elif img.page_number:
        parts.append(f"Figure from page {img.page_number} of {doc.title or doc.doc_id}.")
    return ". ".join(p.rstrip(".") for p in parts if p) + "."


def build_image_chunks(doc: ParsedDoc, images: list[ParsedImage]) -> list[Chunk]:
    """Turn linked images into retrievable `modality=image` chunks.

    Each image is its **own parent section** — it must never be stitched into a
    text section's small-to-big expansion (that would splice a caption into the
    middle of prose). The tie back to the text lives in `linked_section_id`.

    The figure's bytes are copied into the assets tree and the chunk stores the
    resulting *relative* uri, so the index never records a path that only exists
    on the ingesting machine (see `app/core/assets.py`)."""
    citation_base = doc.title or doc.doc_id
    now = datetime.now(UTC).isoformat()
    chunks: list[Chunk] = []
    for i, img in enumerate(images):
        text = sidecar_text(doc, img)
        chunk_id = f"{doc.doc_id}:img{i:03d}"
        label = img.figure_label or img.section or f"page {img.page_number or '?'}"
        # Hash the pixels, not the caption: re-running the linker must not look
        # like new content, but a redrawn diagram must (Tier-4 re-index trigger).
        digest = hashlib.sha256(Path(img.local_path).read_bytes()).hexdigest()[:16]
        image_uri = store_image(doc.doc_id, img.local_path)
        chunks.append(
            Chunk(
                text=text,
                metadata=ChunkMetadata(
                    chunk_id=chunk_id,
                    doc_id=doc.doc_id,
                    source_type=doc.source_type,
                    heading_path=img.heading_path or doc.title,
                    section=img.section or (img.figure_label or "Figure"),
                    chunk_index=i,
                    parent_section_id=chunk_id,   # self-parent; see docstring
                    linked_section_id=img.linked_section_id,
                    content_type=ContentType.DIAGRAM_CAPTION,
                    modality=Modality.IMAGE,
                    image_uri=image_uri,
                    citation_label=f"{citation_base} — {label}",
                    content_hash=digest,
                    ingested_at=now,
                    token_count=count_tokens(text),
                ),
            )
        )
    return chunks
