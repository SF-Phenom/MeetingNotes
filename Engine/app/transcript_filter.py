"""
Transcript filter — collapses repeated/hallucinated segments from transcription output.

Transcription engines can hallucinate repeated lines when they encounter silence
or background noise.  This module provides a post-parse filter that collapses
consecutive duplicate or near-duplicate segments, keeping the first occurrence
and annotating how many were removed.
"""

from __future__ import annotations

import difflib
import logging
import re
import string

logger = logging.getLogger(__name__)

# --- Tunable thresholds ------------------------------------------------------

# How many consecutive repeats before collapsing (normal lines)
EXACT_DUP_THRESHOLD = 3

# For short filler lines (<=3 words like "Right." or "Yeah."), allow more
SHORT_LINE_WORD_LIMIT = 3
SHORT_LINE_DUP_THRESHOLD = 5

# Similarity ratio (0–1) above which two lines are considered near-duplicates
SIMILARITY_THRESHOLD = 0.85

# Translation table for stripping punctuation
_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


# --- Helpers -----------------------------------------------------------------

def _normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for comparison only."""
    text = text.lower().translate(_PUNCT_TABLE)
    return re.sub(r"\s+", " ", text).strip()


def _similarity(a: str, b: str) -> float:
    """Return 0–1 similarity ratio between two normalized strings."""
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# --- Public API --------------------------------------------------------------

def filter_segments(
    segments: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """
    Collapse consecutive duplicate/near-duplicate transcript segments.

    Args:
        segments: list of (timestamp, text) tuples from SRT parsing.

    Returns:
        Filtered list.  Collapsed runs get an annotation on the first segment,
        e.g. ``"I'm going to put it in the pan. [repeated 47 times, removed]"``.
    """
    if not segments:
        return segments

    result: list[tuple[str, str]] = []
    i = 0

    while i < len(segments):
        current_ts, current_text = segments[i]
        current_norm = _normalize(current_text)

        # Count consecutive exact or near-duplicate segments
        j = i + 1
        while j < len(segments):
            next_norm = _normalize(segments[j][1])
            if current_norm == next_norm:
                j += 1
            elif _similarity(current_norm, next_norm) >= SIMILARITY_THRESHOLD:
                j += 1
            else:
                break

        run_length = j - i
        word_count = len(current_text.split())
        threshold = (
            SHORT_LINE_DUP_THRESHOLD
            if word_count <= SHORT_LINE_WORD_LIMIT
            else EXACT_DUP_THRESHOLD
        )

        if run_length > threshold:
            removed = run_length - 1
            annotated = f"{current_text} [repeated {removed} times, removed]"
            result.append((current_ts, annotated))
        else:
            for k in range(i, j):
                result.append(segments[k])

        i = j

    original = len(segments)
    filtered = len(result)
    if original != filtered:
        logger.info(
            "Transcript filter: %d segments -> %d segments (%d removed)",
            original, filtered, original - filtered,
        )

    return result
