"""Speaker-to-sentence alignment for MeetingNotes diarization.

Given Parakeet's per-sentence timings and a diarizer's per-segment speaker
labels, assign one speaker per sentence using max-intersection: the speaker
whose labelled segments overlap the sentence's ``[start, end]`` range most
by total duration wins.

Uses ``intervaltree`` for O(log n + k) segment lookup per sentence (228x
faster than WhisperX's original pandas implementation on long transcripts).

**Granularity note.** This is sentence-level alignment — mid-sentence speaker
changes (rare in 2–4 speaker meetings) get absorbed into whichever speaker
held the floor longest. If real recordings show this is a problem, upgrade
to word-level alignment by exposing Parakeet's per-token timings through
``Sentence`` and running the same interval-tree logic at word granularity.
Non-breaking; no API changes here are needed to support that later.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from intervaltree import IntervalTree

from app.transcript_formatter import Sentence


@dataclass(frozen=True)
class SpeakerSegment:
    """A time range labelled by a diarizer with a single speaker identity.

    ``speaker`` is the user-visible label (e.g. ``"Speaker A"``). Labels are
    local to a single recording — no cross-session continuity is implied.
    """
    start: float
    end: float
    speaker: str


def assign_speakers(
    sentences: list[Sentence],
    segments: list[SpeakerSegment],
) -> list[Sentence]:
    """Return ``sentences`` with ``.speaker`` filled in from ``segments``.

    Each sentence gets the speaker whose segments overlap it most by total
    duration. Sentences with no overlap (or zero-duration sentences) pass
    through unchanged, keeping whatever ``speaker`` they already had — this
    includes the ``None`` default for the common "diarizer never saw this
    moment" case.

    Never mutates the input list; returns a new list of new Sentence
    instances (Sentence is a non-frozen dataclass, but callers downstream
    assume value-semantics).
    """
    if not sentences:
        return []
    if not segments:
        return [replace(s) for s in sentences]

    tree = IntervalTree()
    for seg in segments:
        if seg.end > seg.start:
            tree.addi(seg.start, seg.end, seg.speaker)

    out: list[Sentence] = []
    for sent in sentences:
        if sent.end <= sent.start:
            out.append(replace(sent))
            continue

        totals: dict[str, float] = {}
        for iv in tree[sent.start:sent.end]:
            overlap = min(iv.end, sent.end) - max(iv.begin, sent.start)
            if overlap <= 0:
                continue
            totals[iv.data] = totals.get(iv.data, 0.0) + overlap

        if not totals:
            out.append(replace(sent))
            continue

        # Deterministic tie-break: if two speakers tie on overlap duration,
        # sort by (-overlap, speaker) so the alphabetically-first label wins.
        # Without this, dict iteration order decides and tests get flaky.
        top_speaker = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        out.append(replace(sent, speaker=top_speaker))

    return out
