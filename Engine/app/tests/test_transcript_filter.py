"""Tests for transcript_filter — collapsing repeated/hallucinated segments."""
from __future__ import annotations

from app.transcript_filter import (
    EXACT_DUP_THRESHOLD,
    SHORT_LINE_DUP_THRESHOLD,
    filter_segments,
)


def _seg(n: int, text: str) -> tuple[str, str]:
    """Build a (timestamp, text) tuple with an incrementing timestamp."""
    return (f"00:00:{n:02d}", text)


class TestFilterSegments:
    def test_empty(self):
        assert filter_segments([]) == []

    def test_single_segment_unchanged(self):
        segs = [_seg(0, "Hello world.")]
        assert filter_segments(segs) == segs

    def test_non_duplicate_segments_unchanged(self):
        segs = [
            _seg(0, "Hello world."),
            _seg(1, "How are you today?"),
            _seg(2, "I'm doing fine thanks."),
        ]
        assert filter_segments(segs) == segs

    def test_collapses_long_run_of_exact_duplicates(self):
        # 10 copies of the same line, definitely over threshold.
        dup = "I'm going to put it in the pan."
        segs = [_seg(i, dup) for i in range(10)]

        result = filter_segments(segs)

        assert len(result) == 1
        ts, text = result[0]
        assert ts == "00:00:00"
        assert dup in text
        assert "[repeated 9 times, removed]" in text

    def test_below_threshold_exact_dups_kept(self):
        # EXACT_DUP_THRESHOLD duplicates should be kept as-is.
        dup = "This is a longer sentence that should not be collapsed."
        segs = [_seg(i, dup) for i in range(EXACT_DUP_THRESHOLD)]
        result = filter_segments(segs)
        assert len(result) == EXACT_DUP_THRESHOLD
        assert all("repeated" not in text for _, text in result)

    def test_short_line_higher_threshold(self):
        # Short filler lines (<=3 words) get a higher threshold — e.g. "Right." x4
        # should survive where a long-line run of 4 would be collapsed.
        segs = [_seg(i, "Right.") for i in range(SHORT_LINE_DUP_THRESHOLD)]
        result = filter_segments(segs)
        # At exactly the short-line threshold, kept as-is.
        assert len(result) == SHORT_LINE_DUP_THRESHOLD

    def test_short_line_collapses_above_higher_threshold(self):
        segs = [_seg(i, "Right.") for i in range(SHORT_LINE_DUP_THRESHOLD + 5)]
        result = filter_segments(segs)
        assert len(result) == 1
        assert "repeated" in result[0][1]

    def test_case_and_punctuation_insensitive_dedup(self):
        # Normalization lowercases and strips punctuation before comparing.
        # Use a long phrase (>3 words) so EXACT_DUP_THRESHOLD applies, not the
        # higher short-line threshold.
        segs = [
            _seg(0, "We need to ship this, now!"),
            _seg(1, "we need to ship this now"),
            _seg(2, "WE NEED TO SHIP THIS NOW."),
            _seg(3, "We need to ship this now"),
            _seg(4, "we need, to ship this now."),
        ]
        result = filter_segments(segs)
        # All 5 normalize identically — collapsed (5 > EXACT_DUP_THRESHOLD=3).
        assert len(result) == 1
        assert "repeated 4 times" in result[0][1]

    def test_near_duplicate_collapsed_by_similarity(self):
        # Slight wording drift — SequenceMatcher ratio should exceed threshold.
        segs = [
            _seg(0, "We need to ship the feature by Friday."),
            _seg(1, "We need to ship the feature by Friday"),
            _seg(2, "We need to ship the feature by Friday."),
            _seg(3, "We need to ship the feature by Friday."),
            _seg(4, "We need to ship the feature by Friday."),
        ]
        result = filter_segments(segs)
        assert len(result) == 1

    def test_distinct_segments_after_duplicate_run_preserved(self):
        dup = "Put it in the pan."
        segs = (
            [_seg(i, dup) for i in range(10)]
            + [_seg(10, "Now what's next on the agenda?")]
            + [_seg(11, "Let's move on to budget.")]
        )
        result = filter_segments(segs)
        assert len(result) == 3
        assert "repeated" in result[0][1]
        assert result[1][1] == "Now what's next on the agenda?"
        assert result[2][1] == "Let's move on to budget."
