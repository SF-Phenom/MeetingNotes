"""Tests for speaker_alignment.assign_speakers — interval-tree max-intersection."""
from __future__ import annotations

from app.speaker_alignment import SpeakerSegment, assign_speakers
from app.transcript_formatter import Sentence


def _sent(start: float, end: float, text: str = "hi") -> Sentence:
    return Sentence(start=start, end=end, text=text)


def _seg(start: float, end: float, speaker: str) -> SpeakerSegment:
    return SpeakerSegment(start=start, end=end, speaker=speaker)


class TestAssignSpeakersBasics:
    def test_empty_sentences_returns_empty(self):
        assert assign_speakers([], [_seg(0, 1, "Speaker A")]) == []

    def test_empty_segments_passes_sentences_through_unchanged(self):
        sents = [_sent(0.0, 1.0, "hello")]
        out = assign_speakers(sents, [])
        assert len(out) == 1
        assert out[0].speaker is None
        assert out[0].text == "hello"

    def test_returns_new_list_does_not_mutate_input(self):
        sents = [_sent(0.0, 1.0)]
        out = assign_speakers(sents, [_seg(0.0, 1.0, "Speaker A")])
        # Original sentence still has speaker=None (we never mutated it)
        assert sents[0].speaker is None
        # Output has the label
        assert out[0].speaker == "Speaker A"
        assert out is not sents


class TestSingleSpeakerCoverage:
    def test_full_overlap_single_speaker(self):
        sents = [_sent(0.0, 10.0)]
        segs = [_seg(0.0, 10.0, "Speaker A")]
        assert assign_speakers(sents, segs)[0].speaker == "Speaker A"

    def test_segment_larger_than_sentence(self):
        # Diarizer returns one big block; sentences fall inside it.
        sents = [_sent(5.0, 6.0), _sent(7.0, 8.0)]
        segs = [_seg(0.0, 10.0, "Speaker A")]
        out = assign_speakers(sents, segs)
        assert [s.speaker for s in out] == ["Speaker A", "Speaker A"]

    def test_sentence_outside_all_segments_stays_none(self):
        sents = [_sent(100.0, 101.0)]
        segs = [_seg(0.0, 10.0, "Speaker A")]
        assert assign_speakers(sents, segs)[0].speaker is None


class TestMaxIntersection:
    def test_picks_speaker_with_largest_overlap(self):
        # Sentence spans [0, 10]. Speaker A holds 6s, Speaker B holds 4s.
        sents = [_sent(0.0, 10.0)]
        segs = [_seg(0.0, 6.0, "Speaker A"), _seg(6.0, 10.0, "Speaker B")]
        assert assign_speakers(sents, segs)[0].speaker == "Speaker A"

    def test_picks_different_speakers_for_different_sentences(self):
        sents = [_sent(0.0, 5.0), _sent(5.0, 10.0)]
        segs = [_seg(0.0, 5.0, "Speaker A"), _seg(5.0, 10.0, "Speaker B")]
        out = assign_speakers(sents, segs)
        assert [s.speaker for s in out] == ["Speaker A", "Speaker B"]

    def test_three_speakers_closest_wins(self):
        # Three overlapping speakers; one dominates the sentence.
        sents = [_sent(0.0, 10.0)]
        segs = [
            _seg(0.0, 2.0, "Speaker A"),
            _seg(2.0, 8.0, "Speaker B"),
            _seg(8.0, 10.0, "Speaker C"),
        ]
        assert assign_speakers(sents, segs)[0].speaker == "Speaker B"

    def test_fragmented_same_speaker_sums_across_segments(self):
        # Same label appears in two non-contiguous chunks and must be
        # summed when computing max overlap.
        sents = [_sent(0.0, 10.0)]
        segs = [
            _seg(0.0, 3.0, "Speaker A"),
            _seg(4.0, 5.0, "Speaker B"),
            _seg(5.0, 8.0, "Speaker A"),  # A total = 6s vs B total = 1s
        ]
        assert assign_speakers(sents, segs)[0].speaker == "Speaker A"

    def test_tie_break_is_alphabetical(self):
        # Both speakers have exactly 5s; alphabetical A < B wins.
        sents = [_sent(0.0, 10.0)]
        segs = [_seg(0.0, 5.0, "Speaker B"), _seg(5.0, 10.0, "Speaker A")]
        assert assign_speakers(sents, segs)[0].speaker == "Speaker A"


class TestDegenerateInputs:
    def test_zero_duration_sentence_passes_through(self):
        # end == start sentences have no overlap math; left untouched.
        sents = [Sentence(start=5.0, end=5.0, text="x")]
        segs = [_seg(0.0, 10.0, "Speaker A")]
        assert assign_speakers(sents, segs)[0].speaker is None

    def test_zero_duration_segment_is_ignored(self):
        # end <= start segments contribute no overlap; only valid ones count.
        sents = [_sent(0.0, 10.0)]
        segs = [_seg(5.0, 5.0, "Speaker A"), _seg(0.0, 10.0, "Speaker B")]
        assert assign_speakers(sents, segs)[0].speaker == "Speaker B"

    def test_preserves_other_sentence_fields(self):
        # speech_end and existing speaker field shouldn't be clobbered —
        # the speaker field IS overwritten on assignment, but speech_end,
        # start, end, text must survive untouched.
        sents = [Sentence(start=0.0, end=5.0, text="hi", speech_end=4.5)]
        segs = [_seg(0.0, 5.0, "Speaker A")]
        out = assign_speakers(sents, segs)
        assert out[0].speech_end == 4.5
        assert out[0].start == 0.0
        assert out[0].end == 5.0
        assert out[0].text == "hi"
        assert out[0].speaker == "Speaker A"
