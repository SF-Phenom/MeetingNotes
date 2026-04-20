"""Tests for transcript_formatter — paragraph breaks on speech pauses."""
from __future__ import annotations

from app.transcript_formatter import (
    PARAGRAPH_GAP_SECS,
    SENTENCE_END_GAP_SECS,
    Sentence,
    build_plain_paragraphs,
    build_timestamped_paragraphs,
)


def _sent(start: float, end: float, text: str) -> Sentence:
    return Sentence(start=start, end=end, text=text)  # speech_end defaults to None


class TestBuildPlainParagraphs:
    def test_empty(self):
        assert build_plain_paragraphs([]) == ""

    def test_all_whitespace_filtered(self):
        # Sentences whose text is blank after strip() should drop out.
        assert build_plain_paragraphs([_sent(0.0, 1.0, "   ")]) == ""

    def test_single_sentence(self):
        assert build_plain_paragraphs([_sent(0.0, 1.0, "Hello.")]) == "Hello."

    def test_small_gap_joins_with_space(self):
        # Gap below both thresholds => single space, no paragraph break.
        sentences = [
            _sent(0.0, 1.0, "Hello there"),
            _sent(1.1, 2.0, "how are you"),
        ]
        assert build_plain_paragraphs(sentences) == "Hello there how are you"

    def test_large_gap_breaks_paragraph(self):
        # Mid-sentence gap >= PARAGRAPH_GAP_SECS triggers a break.
        sentences = [
            _sent(0.0, 1.0, "Hello there"),
            _sent(1.0 + PARAGRAPH_GAP_SECS + 0.01, 3.0, "how are you"),
        ]
        result = build_plain_paragraphs(sentences)
        assert result == "Hello there\n\nhow are you"

    def test_short_gap_after_terminal_punct_breaks(self):
        # After ".", the shorter SENTENCE_END_GAP_SECS threshold applies.
        sentences = [
            _sent(0.0, 1.0, "Hello there."),
            _sent(1.0 + SENTENCE_END_GAP_SECS + 0.01, 3.0, "How are you?"),
        ]
        result = build_plain_paragraphs(sentences)
        assert result == "Hello there.\n\nHow are you?"

    def test_short_gap_after_terminal_but_below_short_threshold_joins(self):
        # After ".", gap smaller than SENTENCE_END_GAP_SECS => no break.
        sentences = [
            _sent(0.0, 1.0, "Hello there."),
            _sent(1.0 + SENTENCE_END_GAP_SECS - 0.05, 3.0, "How are you?"),
        ]
        result = build_plain_paragraphs(sentences)
        assert result == "Hello there. How are you?"

    def test_longer_threshold_used_without_terminal_punct(self):
        # A gap that exceeds the short threshold but not the long one: when
        # the previous sentence is NOT terminally-punctuated, the long
        # threshold applies => no break.
        gap = (SENTENCE_END_GAP_SECS + PARAGRAPH_GAP_SECS) / 2
        sentences = [
            _sent(0.0, 1.0, "Hello there"),  # no terminator
            _sent(1.0 + gap, 3.0, "how are you"),
        ]
        result = build_plain_paragraphs(sentences)
        assert result == "Hello there how are you"

    def test_question_and_exclamation_count_as_terminal(self):
        # Both "?" and "!" should trigger the shorter threshold.
        gap = SENTENCE_END_GAP_SECS + 0.05
        for terminator in ("?", "!"):
            first = f"Really{terminator}"
            sentences = [
                _sent(0.0, 1.0, first),
                _sent(1.0 + gap, 3.0, "Yes indeed"),
            ]
            result = build_plain_paragraphs(sentences)
            assert result == f"{first}\n\nYes indeed"

    def test_multiple_paragraphs(self):
        sentences = [
            _sent(0.0, 1.0, "Opening line."),
            _sent(1.1, 2.0, "Same paragraph."),
            _sent(2.0 + PARAGRAPH_GAP_SECS + 0.1, 3.0, "New paragraph here"),
            _sent(3.0 + PARAGRAPH_GAP_SECS + 0.1, 4.0, "Third paragraph"),
        ]
        result = build_plain_paragraphs(sentences)
        # First two join on small gap; last two each start new paragraphs.
        assert result == (
            "Opening line. Same paragraph.\n\nNew paragraph here\n\nThird paragraph"
        )

    def test_negative_or_zero_gap_never_breaks(self):
        # Overlapping sentences (e.g. parakeet timing quirks) should never
        # be treated as paragraph boundaries.
        sentences = [
            _sent(0.0, 5.0, "Long overlapping one."),
            _sent(4.5, 6.0, "Two"),
        ]
        result = build_plain_paragraphs(sentences)
        assert "\n\n" not in result

    def test_strips_per_sentence_whitespace(self):
        sentences = [
            _sent(0.0, 1.0, "  Hello.  "),
            _sent(1.1, 2.0, "  World.  "),
        ]
        assert build_plain_paragraphs(sentences) == "Hello. World."

    def test_speech_end_takes_precedence_over_end(self):
        # Parakeet case: sentence.end absorbs the trailing-silence into the
        # punctuation token's duration, so end == next.start. Using end
        # directly yields gap=0 and no break. speech_end exposes the
        # real-speech endpoint so the pause shows up.
        sentences = [
            Sentence(start=0.0, end=2.0, text="Hello there.", speech_end=1.6),
            Sentence(start=2.0, end=3.0, text="How are you?", speech_end=2.8),
        ]
        result = build_plain_paragraphs(sentences)
        # gap = 2.0 - 1.6 = 0.4s >= SENTENCE_END_GAP_SECS => paragraph break.
        assert result == "Hello there.\n\nHow are you?"

    def test_speech_end_none_falls_back_to_end(self):
        # Apple Speech path: no tokens => speech_end left None. Gap math
        # uses `end` directly, which equals next.start in this contrived
        # case, so no break fires. (Matches the "no paragraph breaks
        # without timing data" graceful-fallback behavior.)
        sentences = [
            Sentence(start=0.0, end=2.0, text="Hello there.", speech_end=None),
            Sentence(start=2.0, end=3.0, text="How are you?", speech_end=None),
        ]
        assert "\n\n" not in build_plain_paragraphs(sentences)


class TestBuildTimestampedParagraphs:
    def test_empty(self):
        assert build_timestamped_paragraphs([]) == ""

    def test_single_sentence_has_timestamp_prefix(self):
        result = build_timestamped_paragraphs([_sent(5.0, 6.0, "Hi.")])
        assert result == "[00:00:05] Hi."

    def test_timestamp_formats_hours_minutes_seconds(self):
        # 1 hour, 2 minutes, 3 seconds in.
        result = build_timestamped_paragraphs(
            [_sent(3723.5, 3724.0, "Late break.")]
        )
        assert result.startswith("[01:02:03] ")

    def test_paragraph_break_is_blank_line(self):
        sentences = [
            _sent(0.0, 1.0, "First."),
            _sent(1.0 + SENTENCE_END_GAP_SECS + 0.1, 3.0, "Second."),
        ]
        result = build_timestamped_paragraphs(sentences)
        lines = result.split("\n")
        # Blank line between the two timestamped lines => paragraph break.
        assert lines == ["[00:00:00] First.", "", "[00:00:01] Second."]

    def test_no_break_when_gap_is_small(self):
        sentences = [
            _sent(0.0, 1.0, "First."),
            _sent(1.05, 2.0, "Second."),
        ]
        result = build_timestamped_paragraphs(sentences)
        assert "\n\n" not in result
        assert result == "[00:00:00] First.\n[00:00:01] Second."


# ---------------------------------------------------------------------------
# Speaker prefixes (diarization plumbing)
# ---------------------------------------------------------------------------


def _speaker_sent(start: float, end: float, text: str, speaker: str | None) -> Sentence:
    return Sentence(start=start, end=end, text=text, speaker=speaker)


class TestBuildPlainParagraphsWithSpeakers:
    def test_no_speakers_anywhere_matches_legacy_output(self):
        # Baseline: when no sentence has a speaker, output is byte-identical
        # to the pre-diarization behavior. Critical for the Apple Speech
        # path and for all existing Parakeet recordings.
        sentences = [
            _sent(0.0, 1.0, "Opening."),
            _sent(1.0 + PARAGRAPH_GAP_SECS + 0.1, 3.0, "New paragraph"),
        ]
        assert build_plain_paragraphs(sentences) == "Opening.\n\nNew paragraph"

    def test_single_speaker_prefixed_on_first_paragraph(self):
        sentences = [
            _speaker_sent(0.0, 1.0, "Hello.", "Speaker A"),
            _speaker_sent(1.1, 2.0, "how are you", "Speaker A"),
        ]
        # Same paragraph (small gap, same speaker) — single prefix at the top.
        assert build_plain_paragraphs(sentences) == "**Speaker A:** Hello. how are you"

    def test_speaker_change_forces_paragraph_break(self):
        # Adjacent sentences with different speakers MUST break into separate
        # paragraphs even when the pause gap is below threshold.
        sentences = [
            _speaker_sent(0.0, 1.0, "Hello.", "Speaker A"),
            _speaker_sent(1.05, 2.0, "Hi there.", "Speaker B"),
        ]
        result = build_plain_paragraphs(sentences)
        assert result == "**Speaker A:** Hello.\n\n**Speaker B:** Hi there."

    def test_speaker_prefix_on_each_paragraph(self):
        sentences = [
            _speaker_sent(0.0, 1.0, "First.", "Speaker A"),
            _speaker_sent(1.0 + PARAGRAPH_GAP_SECS + 0.1, 3.0, "Second.", "Speaker A"),
            _speaker_sent(3.0 + PARAGRAPH_GAP_SECS + 0.1, 4.0, "Third.", "Speaker B"),
        ]
        result = build_plain_paragraphs(sentences)
        # A-pause-A-pause-B: three paragraphs, three prefixes.
        assert result == (
            "**Speaker A:** First.\n\n"
            "**Speaker A:** Second.\n\n"
            "**Speaker B:** Third."
        )

    def test_none_speaker_emits_no_prefix(self):
        # Mixed None/Some should not crash; the None sentence just skips the
        # prefix while still honoring its pause-based paragraph boundary.
        sentences = [
            _speaker_sent(0.0, 1.0, "Anon.", None),
            _speaker_sent(1.0 + PARAGRAPH_GAP_SECS + 0.1, 3.0, "Known.", "Speaker A"),
        ]
        result = build_plain_paragraphs(sentences)
        assert result == "Anon.\n\n**Speaker A:** Known."

    def test_speaker_change_requires_both_sides_labelled(self):
        # A speaker "change" only counts when BOTH sentences have a label.
        # None -> "A" is "label appeared," not a change — with a small pause
        # gap, the sentences stay in one paragraph and no prefix is emitted
        # mid-paragraph even though the second sentence has a speaker.
        sentences = [
            _speaker_sent(0.0, 1.0, "Anon.", None),
            _speaker_sent(1.05, 2.0, "Known.", "Speaker A"),
        ]
        assert build_plain_paragraphs(sentences) == "Anon. Known."


class TestBuildTimestampedParagraphsWithSpeakers:
    def test_no_speakers_matches_legacy(self):
        sentences = [_sent(5.0, 6.0, "Hi.")]
        assert build_timestamped_paragraphs(sentences) == "[00:00:05] Hi."

    def test_speaker_prefix_after_timestamp(self):
        sentences = [_speaker_sent(5.0, 6.0, "Hi.", "Speaker A")]
        assert (
            build_timestamped_paragraphs(sentences) == "[00:00:05] **Speaker A:** Hi."
        )

    def test_speaker_change_breaks_and_reprefixes(self):
        sentences = [
            _speaker_sent(0.0, 1.0, "Hello.", "Speaker A"),
            _speaker_sent(1.05, 2.0, "Hi there.", "Speaker B"),
        ]
        result = build_timestamped_paragraphs(sentences)
        assert result.split("\n") == [
            "[00:00:00] **Speaker A:** Hello.",
            "",
            "[00:00:01] **Speaker B:** Hi there.",
        ]

    def test_prefix_not_repeated_within_paragraph(self):
        # Two sentences, same speaker, small gap: both get timestamps but
        # only the FIRST line gets the speaker prefix (it's paragraph-start).
        sentences = [
            _speaker_sent(0.0, 1.0, "First.", "Speaker A"),
            _speaker_sent(1.05, 2.0, "Second.", "Speaker A"),
        ]
        result = build_timestamped_paragraphs(sentences)
        assert result.split("\n") == [
            "[00:00:00] **Speaker A:** First.",
            "[00:00:01] Second.",
        ]
