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
    """One sentence with absolute start/end times in seconds.

    ``speech_end`` is the end time of the last *spoken* token, excluding
    any trailing pure-punctuation tokens. Parakeet absorbs the speaker's
    end-of-sentence silence into the duration of the terminal punctuation
    token (e.g. a "." token can stretch 300 ms for a real sentence break),
    so using ``end`` directly for gap math always gives zero. When
    ``speech_end`` is populated, the paragraph-break logic uses it as the
    "real speech stopped here" reference instead. Left as ``None`` for
    engines without token-level timings (Apple Speech), in which case
    gap math falls back to ``end`` and simply never triggers — paragraph
    breaks are a Parakeet-only feature.

    ``speaker`` holds the diarization label (e.g. "Speaker A") assigned
    by ``speaker_alignment.assign_speakers`` when diarization ran on the
    recording. ``None`` means diarization was disabled or the sentence
    had no overlap with any diarizer segment; the formatter falls back
    to its non-speaker behavior in both cases.
    """
    start: float
    end: float
    text: str
    speech_end: float | None = None
    speaker: str | None = None


def _pause_end(sent: Sentence) -> float:
    """Return the timestamp to use as ``sent``'s pause-boundary reference."""
    return sent.speech_end if sent.speech_end is not None else sent.end


def _is_pause_boundary(prev: Sentence, cur: Sentence) -> bool:
    gap = cur.start - _pause_end(prev)
    if gap <= 0:
        return False
    if prev.text.rstrip().endswith(SENTENCE_TERMINATORS):
        return gap >= SENTENCE_END_GAP_SECS
    return gap >= PARAGRAPH_GAP_SECS


def _is_speaker_change(prev: Sentence, cur: Sentence) -> bool:
    # A speaker change only counts when both sentences have a label —
    # a None on either side is "unknown speaker here," not a real change.
    if prev.speaker is None or cur.speaker is None:
        return False
    return prev.speaker != cur.speaker


def _is_paragraph_boundary(prev: Sentence, cur: Sentence) -> bool:
    return _is_pause_boundary(prev, cur) or _is_speaker_change(prev, cur)


def _format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"[{h:02d}:{m:02d}:{s:02d}]"


def _speaker_prefix(sent: Sentence) -> str:
    """Bold speaker label emitted at the start of each speaker-owned paragraph."""
    if sent.speaker is None:
        return ""
    return f"**{sent.speaker}:** "


def build_plain_paragraphs(sentences: list[Sentence]) -> str:
    """Join sentences with ``\\n\\n`` on paragraph boundaries, single space otherwise.

    A paragraph boundary is a pause gap OR a speaker change. When a sentence
    has ``speaker`` set, a ``**Speaker X:** `` prefix is emitted at the
    start of each paragraph. Sentences without a speaker render as before.
    """
    cleaned = [s for s in sentences if s.text.strip()]
    if not cleaned:
        return ""
    parts: list[str] = [_speaker_prefix(cleaned[0]) + cleaned[0].text.strip()]
    for prev, cur in zip(cleaned, cleaned[1:]):
        if _is_paragraph_boundary(prev, cur):
            parts.append("\n\n" + _speaker_prefix(cur) + cur.text.strip())
        else:
            parts.append(" " + cur.text.strip())
    return "".join(parts)


def build_timestamped_paragraphs(sentences: list[Sentence]) -> str:
    """One timestamped line per sentence; blank line between paragraphs.

    Blank lines map to ``\\n\\n`` in the rendered markdown, which every
    reasonable renderer treats as a real paragraph break. Paragraph
    boundaries fire on pause gaps OR speaker changes; when a sentence
    starts a new paragraph and has a ``speaker`` set, its line is prefixed
    with ``**Speaker X:** `` after the timestamp.
    """
    cleaned = [s for s in sentences if s.text.strip()]
    if not cleaned:
        return ""
    lines: list[str] = []
    for i, sent in enumerate(cleaned):
        is_new_paragraph = i == 0 or _is_paragraph_boundary(cleaned[i - 1], sent)
        if i > 0 and is_new_paragraph:
            lines.append("")
        prefix = _speaker_prefix(sent) if is_new_paragraph else ""
        lines.append(f"{_format_timestamp(sent.start)} {prefix}{sent.text.strip()}")
    return "\n".join(lines)
