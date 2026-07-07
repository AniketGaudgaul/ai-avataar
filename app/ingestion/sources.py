"""Source-document loading + provenance marking (spec 5.3 step 1).

The graph is built from resume + narrative *together*, but every extracted fact
must keep its provenance. So each document is wrapped in a source marker
(`[RESUME] ... [/RESUME]`, `[NARRATIVE] ... [/NARRATIVE]`) before being handed
to the extractor, and the extractor is asked to tag each entity/edge with the
block it came from.

For this first build only the resume exists (`temp_data/main.md`). The narrative
slots in with zero code changes: drop `temp_data/narrative.md` and it is picked
up automatically. The same `SourceDoc` abstraction will feed the vector path.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.ingestion.graph.schema import SourceType

logger = get_logger(__name__)

# Project root = three levels up from this file (app/ingestion/sources.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Default source registry: (source type, path relative to project root).
# Only files that exist are loaded, so the narrative is optional until written.
DEFAULT_SOURCES: list[tuple[SourceType, str]] = [
    (SourceType.RESUME, "temp_data/main.md"),
    (SourceType.NARRATIVE, "temp_data/narrative.md"),
]


@dataclass(frozen=True)
class SourceDoc:
    source_type: SourceType
    doc_id: str          # canonical id, e.g. "resume"
    path: Path
    text: str

    @property
    def marker(self) -> str:
        return self.source_type.value.upper()

    def marked(self) -> str:
        """The text wrapped in its provenance marker block."""
        return f"[{self.marker}]\n{self.text.strip()}\n[/{self.marker}]"


def load_sources(
    entries: list[tuple[SourceType, str]] | None = None,
) -> list[SourceDoc]:
    """Load every source file that exists, in the given order.

    Missing files are skipped with a log line (the narrative is expected to be
    absent early on). Raises if *no* sources were found at all.
    """
    entries = entries or DEFAULT_SOURCES
    docs: list[SourceDoc] = []
    for source_type, rel in entries:
        path = (PROJECT_ROOT / rel).resolve()
        if not path.exists():
            logger.info("source skipped (not found)", extra={"path": str(path)})
            continue
        text = path.read_text(encoding="utf-8")
        docs.append(
            SourceDoc(
                source_type=source_type,
                doc_id=source_type.value,
                path=path,
                text=text,
            )
        )
        logger.info(
            "source loaded",
            extra={"source_type": source_type.value, "path": str(path), "chars": len(text)},
        )
    if not docs:
        raise FileNotFoundError(
            "No source documents found. Expected at least temp_data/main.md "
            f"under {PROJECT_ROOT}."
        )
    return docs


def build_marked_corpus(docs: list[SourceDoc]) -> str:
    """Concatenate all sources, each wrapped in its provenance marker block."""
    return "\n\n".join(doc.marked() for doc in docs)
