"""Approximate token counting for chunk-boundary decisions.

Gemini exposes no local tokenizer, and `count_tokens` is a network call — too slow
and quota-hungry to run per candidate chunk boundary. Chunk *sizing* does not need
exactness, only a consistent estimate, so we use a cheap local heuristic behind a
single seam. Swap `count_tokens` for tiktoken or the Gemini counter later without
touching the chunker.

NOTE: these counts are ~10-20% off Gemini's real tokenization — fine for deciding
where to split, but do not treat them as ground truth for context-window budgeting.
"""

from __future__ import annotations


def count_tokens(text: str) -> int:
    """Estimate tokens as a blend of a char-based (~4 chars/token) and a
    word-based (~1.3 tokens/word) estimate — the average tracks real BPE counts
    on mixed prose/code better than either alone."""
    if not text:
        return 0
    chars = len(text)
    words = len(text.split())
    return max(1, round((chars / 4 + words * 1.3) / 2))
