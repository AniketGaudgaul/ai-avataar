"""Figures in the answer loop (spec 5.7, 6.3) — showing them to the model, and
letting the model show them to the user.

Retrieval already surfaces figures next to prose. This module is how they reach
the specialist and, sometimes, the reply.

Three jobs:

1. **Show the pixels.** Each retrieved figure is inlined into the generation
   request as image bytes, labelled `[img1]`, `[img2]`. Gemini is natively
   multimodal, so the specialist reads the diagram itself rather than the
   one-line caption that stands in for it — a boxes-and-arrows figure answers
   "what calls what" in a way its caption never does.

2. **Let the model decide.** A figure appears in the answer only if the model
   writes its marker. Retrieval proposes; the generator disposes. This split is
   forced by how images are anchored: a figure inherits the relevance of the
   *section* it sits in (`retrieval/vector.py`), so a genuinely relevant section
   drags along whatever else it owns — the two-phase-pipeline section carries
   both the pipeline diagram and a UI screenshot. No score separates them,
   because they were never scored against the query. Only something holding both
   the question and the picture can tell them apart, and that is the model.

3. **Trust no marker.** A model that invents `[img7]` when two figures were shown
   would leave a dangling reference in the API response, so unknown markers are
   stripped before the answer leaves the node.

Markers live in a namespace separate from citations: `[img1]` is a figure, `[1]`
is a source. The citation regexes require all-digit brackets, so the two can
never collide and a figure never consumes a citation number that the guardrail
would then demand a source for.
"""

from __future__ import annotations

import re

from pydantic import BaseModel

from app.config import settings
from app.core.assets import mime_for, resolve
from app.core.gemini import ImagePart
from app.core.logging import get_logger
from app.retrieval.schema import RetrievedImage

logger = get_logger(__name__)

_IMAGE_MARKER_RE = re.compile(r"\[img(\d+)\]", re.IGNORECASE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")
_SPACE_RUN_RE = re.compile(r"[ \t]{2,}")


class ChosenFigure(BaseModel):
    """A figure the answer references, paired with the marker it references it by.

    The marker is positional in the list of figures *shown* to the model, which is
    not the list of figures it chose — so it has to be captured here, while both
    lists are in hand, rather than recovered downstream.
    """

    marker: str
    image: RetrievedImage


def marker(index: int) -> str:
    """The marker for the 1-based `index`-th shown figure."""
    return f"[img{index}]"


def loadable(images: list[RetrievedImage]) -> list[RetrievedImage]:
    """Drop figures whose bytes cannot be shown to the model.

    Filtering happens *before* numbering, deliberately: markers are positional, so
    a figure silently skipped at load time would shift every later marker and make
    the model cite the wrong picture.
    """
    kept: list[RetrievedImage] = []
    for img in images:
        path = resolve(img.image_uri)
        if path is None:
            logger.warning(
                "figure missing on disk",
                extra={"chunk_id": img.chunk_id, "uri": img.image_uri},
            )
            continue
        if path.stat().st_size > settings.agent_image_max_bytes:
            logger.warning("figure too large to show", extra={"chunk_id": img.chunk_id})
            continue
        kept.append(img)
    return kept


def figure_block(images: list[RetrievedImage]) -> str:
    """The prompt block that names each shown figure and its caption."""
    if not images:
        return ""
    entries = "\n\n".join(
        f"{marker(i)} ({img.citation_label})\n{img.caption}"
        for i, img in enumerate(images, 1)
    )
    return (
        "FIGURES (these images are attached above — you can see them. Include one "
        "in your answer by writing its marker on its own line):\n" + entries
    )


def load_image_parts(images: list[RetrievedImage]) -> list[ImagePart]:
    """Read each figure's bytes into a labelled `ImagePart` for the request.

    Assumes `loadable` already filtered the list; a file that disappears between
    the two is skipped with a warning rather than failing the turn.
    """
    parts: list[ImagePart] = []
    for i, img in enumerate(images, 1):
        path = resolve(img.image_uri)
        if path is None:
            continue  # raced with `loadable`; already logged there
        try:
            data = path.read_bytes()
        except OSError as exc:
            logger.warning("figure unreadable", extra={"chunk_id": img.chunk_id, "error": str(exc)})
            continue
        parts.append(
            ImagePart(
                data=data,
                mime_type=mime_for(path),
                label=f"{marker(i)} — {img.citation_label}: {img.caption}",
            )
        )
    return parts


def strip_unknown_markers(answer: str, shown: int) -> str:
    """Remove `[imgN]` markers that point at no shown figure."""

    def keep(match: re.Match[str]) -> str:
        return match.group(0) if 1 <= int(match.group(1)) <= shown else ""

    cleaned = _IMAGE_MARKER_RE.sub(keep, answer)
    # Removing a marker leaves the whitespace that surrounded it.
    cleaned = _SPACE_RUN_RE.sub(" ", cleaned)
    return _BLANK_RUN_RE.sub("\n\n", cleaned).strip()


def used_images(answer: str, images: list[RetrievedImage]) -> list[ChosenFigure]:
    """The figures the answer actually references, in marker order.

    Only these are returned to the client — a figure shown to the model but not
    chosen by it never reaches the user. Assumes unknown markers were already
    stripped, so every index resolves.
    """
    indices = {int(m) for m in _IMAGE_MARKER_RE.findall(answer)}
    return [
        ChosenFigure(marker=marker(i), image=images[i - 1])
        for i in sorted(indices)
        if 1 <= i <= len(images)
    ]
