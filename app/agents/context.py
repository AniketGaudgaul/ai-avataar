"""Context assembly — turn retrieved vector contexts + graph facts into one
numbered, citation-labelled block for the specialist prompts (spec 4.1 step 5).

Two jobs:
1. **Assemble** — vector contexts become `[1..n]`, graph facts collapse into a
   single trailing `[n+1]` block. Each numbered entry has a matching `Citation`
   so the generator can cite by number and the guardrail can trace every claim.
2. **Budget** — parent sections can be ~8k chars (retrieval checkpoint), so cap
   the number of contexts and truncate each to keep the prompt bounded.

`used_citations` filters the full citation set down to the markers that actually
appear in the final answer — that's what the API returns to the UI.
"""

from __future__ import annotations

import re

from app.agents.state import Citation
from app.config import settings
from app.retrieval.schema import GraphFact, RetrievedContext

# Matches "[3]" and the grouped form models reach for unprompted, "[2, 5]".
# Anchoring on all-digit brackets keeps figure markers ("[img1]") out.
_MARKER_RE = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")

_LABEL_SEP = " — "


def _fix_mojibake(s: str) -> str:
    """Repair the common UTF-8-decoded-as-Latin-1 corruption (e.g. "â€"" → "—").

    Some Qdrant points were written with a mangled em-dash; rather than re-ingest,
    undo the round-trip at the boundary. Only attempted when a tell-tale marker is
    present, and reverted if the re-decode fails — so clean labels are untouched.
    """
    if not any(tell in s for tell in ("Ã", "â€", "Â")):
        return s
    # The corruption is UTF-8 bytes decoded as Windows-1252 (its 0x80-0x9F range,
    # e.g. "€"/"”", is why plain latin-1 can't reverse it) — so re-encode cp1252.
    try:
        return s.encode("cp1252").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return s


def clean_label(label: str) -> str:
    """Tidy a stored citation label for display.

    Ingest builds labels as "Doc Title — Section", which reads badly in a chip
    when the section repeats the title ("X — X") or the title is a long
    "Short: long academic subtitle". Collapse the duplication and keep the short
    title, so a chip reads "MedSumm — Table 1" instead of the full paper name
    twice. Purely cosmetic; the retrieval and citation logic are untouched.
    """
    s = _fix_mojibake(label or "").strip()
    parts = [p.strip() for p in s.split(_LABEL_SEP) if p.strip()]
    if not parts:
        return s

    # Collapse adjacent duplicate segments first ("X — X" → "X").
    deduped: list[str] = []
    for p in parts:
        if not deduped or deduped[-1].casefold() != p.casefold():
            deduped.append(p)

    # Then shorten a long "Title: subtitle" head to just the title.
    head = deduped[0]
    if len(head) > 40 and ":" in head:
        short = head.split(":", 1)[0].strip()
        if 2 <= len(short) <= 40:
            deduped[0] = short

    return _LABEL_SEP.join(deduped)


def assemble_context(
    contexts: list[RetrievedContext],
    graph_facts: list[GraphFact],
    *,
    max_contexts: int | None = None,
    max_chars: int | None = None,
) -> tuple[str, list[Citation]]:
    """Return `(context_block, citations)` where `citations[i]` labels marker
    `[i+1]`. Vector contexts come first; graph facts collapse into one trailing
    numbered block."""
    max_contexts = max_contexts if max_contexts is not None else settings.agent_max_contexts
    max_chars = max_chars if max_chars is not None else settings.agent_max_context_chars

    blocks: list[str] = []
    citations: list[Citation] = []

    for c in contexts[:max_contexts]:
        n = len(citations) + 1
        text = c.text.strip()
        if len(text) > max_chars:
            text = text[:max_chars].rstrip() + " …[truncated]"
        label = clean_label(c.citation_label)
        citations.append(
            Citation(label=label, source_type=c.source_type, ref=c.parent_section_id)
        )
        blocks.append(f"[{n}] ({label})\n{text}")

    if graph_facts:
        n = len(citations) + 1
        lines = "\n".join(f"- {f.as_sentence()}" for f in graph_facts)
        src_docs = sorted({d for f in graph_facts for d in f.source_docs})
        citations.append(
            Citation(label="Knowledge graph", source_type="graph", ref=",".join(src_docs))
        )
        blocks.append(f"[{n}] (Knowledge graph facts)\n{lines}")

    return "\n\n".join(blocks), citations


def used_citations(answer: str, citations: list[Citation]) -> list[Citation]:
    """Filter `citations` to only those whose `[n]` marker appears in `answer`,
    in source order. Handles grouped markers ("[2, 5]" cites both 2 and 5)."""
    used_idx = {
        int(part) for group in _MARKER_RE.findall(answer) for part in group.split(",")
    }
    return [citations[i - 1] for i in sorted(used_idx) if 1 <= i <= len(citations)]
