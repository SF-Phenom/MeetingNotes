"""
Transcript formatter — insert paragraph breaks on speech pauses.

Parakeet emits per-sentence start/end timestamps. A large gap between
one sentence's end and the next sentence's start is overwhelmingly a
speaker yielding the floor, so inserting ``\\n\\n`` at those boundaries
produces much more readable transcripts without the weight of real
speaker diarization.

Tune ``PARAGRAPH_GAP_SECS`` / ``SENTENCE_END_GAP_SECS`` against real
recordings — the current values are informed guesses.
"""

from __future__ import annotations

from dataclasses import dataclass


# Default silence (seconds) between two sentences that counts as a
# paragraph boundary. 800 ms is long enough to avoid breaking on normal
# within-sentence in-breaths but short enough to catch speaker yields.
PARAGRAPH_GAP_SECS = 0.8

# Shorter threshold when the previous sentence ended with terminal
# punctuation — a sentence-final pause is already a stronger cue, so we
# accept a smaller gap as a paragraph break.
SENTENCE_END_GAP_SECS = 0.3

# Characters that, when they end a sentence, enable the shorter threshold.
SENTENCE_TERMINATORS = (".", "?", "!")


@dataclass
class Sentence:
    """One sentence with absolute start/end times in seconds."""
    start: float
    end: float
    text: str


def _is_paragraph_boundary(prev: Sentence, cur: Sentence) -> bool:
    gap = cur.start - prev.end
    if gap <= 0:
        return False
    if prev.text.rstrip().endswith(SENTENCE_TERMINATORS):
        return gap >= SENTENCE_END_GAP_SECS
    return gap >= PARAGRAPH_GAP_SECS


def _format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def build_plain_paragraphs(sentences: list[Sentence]) -> str:
    """Join sentences with ``\\n\\n`` on pause boundaries, single space otherwise."""
    cleaned = [s for s in sentences if s.text.strip()]
    if not cleaned:
        return ""
    parts: list[str] = [cleaned[0].text.strip()]
    for prev, cur in zip(cleaned, cleaned[1:]):
        sep = "\n\n" if _is_paragraph_boundary(prev, cur) else " "
        parts.append(sep + cur.text.strip())
    return "".join(parts)


def build_timestamped_paragraphs(sentences: list[Sentence]) -> str:
    """One timestamped line per sentence; blank line between paragraphs.

    Blank lines map to ``\\n\\n`` in the rendered markdown, which every
    reasonable renderer treats as a real paragraph break.
    """
    cleaned = [s for s in sentences if s.text.strip()]
    if not cleaned:
        return ""
    lines: list[str] = []
    for i, sent in enumerate(cleaned):
        if i > 0 and _is_paragraph_boundary(cleaned[i - 1], sent):
            lines.append("")
        lines.append(f"{_format_timestamp(sent.start)} {sent.text.strip()}")
    return "\n".join(lines)
