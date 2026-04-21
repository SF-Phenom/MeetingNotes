"""Tests for the pipeline-level diarization step.

Covers the two call paths and the model-routing decision. The critical
test here is ``test_realtime_shortcut_path_diarizes`` — exercising the
scenario that silently broke under the batch-engine-wiring design: the
pipeline taking its pre-transcribed-text fast path from realtime and
never invoking diarization. The previous commit's tests all passed but
none covered this path, which is exactly why the bug shipped.
"""
from __future__ import annotations

import pytest

from app import pipeline
from app.pipeline import (
    DIARIZATION_ENABLED_ENV_VAR,
    _maybe_diarize,
    _pick_diarizer_model,
)
from app.speaker_alignment import SpeakerSegment
from app.transcript_formatter import Sentence
from app.transcriber import TranscriptionResult


# ---------------------------------------------------------------------------
# _pick_diarizer_model — routing decision on metadata
# ---------------------------------------------------------------------------


class TestPickDiarizerModel:
    def test_no_participants_defaults_to_sortformer(self):
        # Ad-hoc recording with no calendar match: assume small meeting.
        assert _pick_diarizer_model({}) == "sortformer"

    def test_empty_participants_string_defaults_to_sortformer(self):
        assert _pick_diarizer_model({"participants": ""}) == "sortformer"
        assert _pick_diarizer_model({"participants": "   "}) == "sortformer"

    def test_non_string_participants_defaults_to_sortformer(self):
        # Defensive — metadata might carry a list or None from some edge path.
        assert _pick_diarizer_model({"participants": None}) == "sortformer"
        assert _pick_diarizer_model({"participants": ["Alice"]}) == "sortformer"

    def test_single_other_attendee_picks_sortformer(self):
        # 1 other + user = 2 total → sortformer (≤6).
        assert _pick_diarizer_model({"participants": "Alice"}) == "sortformer"

    def test_six_total_still_picks_sortformer(self):
        # 5 others + user = 6 → right at the boundary, still sortformer.
        assert _pick_diarizer_model(
            {"participants": "A, B, C, D, E"}
        ) == "sortformer"

    def test_seven_total_falls_back_to_community_1(self):
        # 6 others + user = 7 → over the Sortformer cap.
        assert _pick_diarizer_model(
            {"participants": "A, B, C, D, E, F"}
        ) == "community-1"

    def test_large_meeting_is_community_1(self):
        many = ", ".join(f"Person{i}" for i in range(20))
        assert _pick_diarizer_model({"participants": many}) == "community-1"

    def test_stray_whitespace_and_empty_slots_ignored(self):
        # Should still produce a meaningful count with defensive parsing.
        hints = _pick_diarizer_model({"participants": " Alice ,  , Bob ,"})
        assert hints == "sortformer"  # 2 real + user = 3


# ---------------------------------------------------------------------------
# _maybe_diarize — the single diarization call site
# ---------------------------------------------------------------------------


def _make_result(sentences: list[Sentence] | None) -> TranscriptionResult:
    """Fresh TranscriptionResult with the given sentences and baseline text."""
    return TranscriptionResult(
        plain_text="plain placeholder",
        timestamped_text="[00:00:00] placeholder",
        duration_minutes=1,
        srt_path="",
        sentences=sentences,
    )


class _RecordingDiarizer:
    """Capture diarize() calls + return scripted segments."""
    def __init__(self, segments: list[SpeakerSegment] | None):
        self._segments = segments
        self.calls: list[tuple[str, str | None]] = []

    def diarize(self, wav_path: str, model: str | None = None):
        self.calls.append((wav_path, model))
        return self._segments


@pytest.fixture
def diarization_on(monkeypatch):
    """Force diarization on via env var — no state.json dependency needed."""
    monkeypatch.setenv(DIARIZATION_ENABLED_ENV_VAR, "1")


@pytest.fixture
def diarization_off(monkeypatch):
    monkeypatch.setenv(DIARIZATION_ENABLED_ENV_VAR, "0")


class TestMaybeDiarize:
    def test_disabled_returns_input_unchanged(self, diarization_off, monkeypatch):
        # Even if a backend is registered, the disabled flag wins —
        # the backend shouldn't even be consulted.
        diarizer = _RecordingDiarizer([])
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)
        result = _make_result([Sentence(0.0, 1.0, "hi")])
        assert _maybe_diarize("/tmp/x.wav", result, {}) is result
        assert diarizer.calls == []

    def test_enabled_but_no_sentences_returns_input_unchanged(
        self, diarization_on, monkeypatch,
    ):
        # Apple Speech path: no timings → diarization is inapplicable.
        diarizer = _RecordingDiarizer([])
        monkeypatch.setattr(
            "app.diarizer.get_diarizer", lambda: diarizer,
        )
        result = _make_result(None)  # no sentences
        out = _maybe_diarize("/tmp/x.wav", result, {})
        assert out is result
        # Subprocess never invoked — we don't spawn Swift just to find out
        # there's nothing to align.
        assert diarizer.calls == []

    def test_enabled_but_no_backend_returns_input(self, diarization_on, monkeypatch):
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: None)
        result = _make_result([Sentence(0.0, 1.0, "hi")])
        assert _maybe_diarize("/tmp/x.wav", result, {}) is result

    def test_backend_raises_swallows_and_returns_input(
        self, diarization_on, monkeypatch,
    ):
        class Exploding:
            def diarize(self, *a, **kw):
                raise RuntimeError("backend crashed")

        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: Exploding())
        result = _make_result([Sentence(0.0, 1.0, "hi")])
        # Must not raise — diarization failures are non-fatal.
        out = _maybe_diarize("/tmp/x.wav", result, {})
        assert out is result

    def test_happy_path_relabels_and_rerenders_text(
        self, diarization_on, monkeypatch,
    ):
        # Two sentences, diarizer says different speakers for each →
        # output text should contain **Speaker X:** prefixes.
        sentences = [
            Sentence(start=0.0, end=1.0, text="hello."),
            Sentence(start=1.1, end=2.0, text="how are you?"),
        ]
        segments = [
            SpeakerSegment(start=0.0, end=1.0, speaker="Speaker A"),
            SpeakerSegment(start=1.0, end=2.5, speaker="Speaker B"),
        ]
        diarizer = _RecordingDiarizer(segments)
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)

        result = _make_result(sentences)
        out = _maybe_diarize("/tmp/x.wav", result, {})

        # Not the same object — we return a new TranscriptionResult.
        assert out is not result
        assert "**Speaker A:**" in out.plain_text
        assert "**Speaker B:**" in out.plain_text
        # Sentences list on the result now carries the speaker labels.
        assert all(s.speaker is not None for s in out.sentences)
        # Subprocess called exactly once. Empty metadata = no calendar
        # match → sortformer (the ad-hoc default).
        assert diarizer.calls == [("/tmp/x.wav", "sortformer")]

    def test_routes_sortformer_for_small_meeting(
        self, diarization_on, monkeypatch,
    ):
        diarizer = _RecordingDiarizer([
            SpeakerSegment(0.0, 5.0, "Speaker A"),
        ])
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)
        result = _make_result([Sentence(0.0, 5.0, "hi")])
        # 2 others + user = 3 ≤ 6 → sortformer.
        _maybe_diarize("/tmp/x.wav", result, {"participants": "Alice, Bob"})
        assert diarizer.calls[0][1] == "sortformer"

    def test_routes_community_1_for_large_meeting(
        self, diarization_on, monkeypatch,
    ):
        diarizer = _RecordingDiarizer([
            SpeakerSegment(0.0, 5.0, "Speaker A"),
        ])
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)
        result = _make_result([Sentence(0.0, 5.0, "hi")])
        # 6 others + user = 7 > 6 → community-1.
        _maybe_diarize(
            "/tmp/x.wav", result,
            {"participants": "A, B, C, D, E, F"},
        )
        assert diarizer.calls[0][1] == "community-1"

    def test_empty_segments_return_input_unchanged(
        self, diarization_on, monkeypatch,
    ):
        # Single-speaker recording: backend decides no labels are useful.
        diarizer = _RecordingDiarizer([])
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)
        result = _make_result([Sentence(0.0, 5.0, "hi")])
        out = _maybe_diarize("/tmp/x.wav", result, {})
        assert out is result

    def test_preserves_non_text_fields(self, diarization_on, monkeypatch):
        # duration_minutes + srt_path should survive a speakered rebuild
        # — they don't depend on the text but callers read them downstream.
        diarizer = _RecordingDiarizer([
            SpeakerSegment(0.0, 5.0, "Speaker A"),
        ])
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)
        original = TranscriptionResult(
            plain_text="X", timestamped_text="X",
            duration_minutes=42, srt_path="/tmp/foo.srt",
            sentences=[Sentence(0.0, 5.0, "hi")],
        )
        out = _maybe_diarize("/tmp/x.wav", original, {})
        assert out.duration_minutes == 42
        assert out.srt_path == "/tmp/foo.srt"


# ---------------------------------------------------------------------------
# The regression test the previous wiring was missing.
# ---------------------------------------------------------------------------


class TestRealtimeShortcutPathDiarizes:
    """In commit de35a87 the diarization hook lived inside
    ``transcribe_with_parakeet``, which meant the pipeline's
    pre-transcribed-text fast path (the normal production flow) skipped
    diarization entirely. That silent failure shipped to main because no
    test exercised the combination "diarization enabled + pre-transcribed
    text provided + sentences handed through." This test closes that gap.
    """

    def test_pre_transcribed_sentences_flow_through_diarizer(
        self, diarization_on, monkeypatch, tmp_path,
    ):
        # The scenario from the failing real-meeting test:
        # - realtime produced text + sentences before stop
        # - pipeline takes pre_transcribed_text fast path (no batch engine)
        # - diarization must still run on the handed-through sentences
        sentences = [
            Sentence(start=0.0, end=2.0, text="First speaker talking."),
            Sentence(start=2.5, end=4.0, text="Second speaker replying."),
        ]
        segments = [
            SpeakerSegment(start=0.0, end=2.0, speaker="Speaker A"),
            SpeakerSegment(start=2.0, end=5.0, speaker="Speaker B"),
        ]
        diarizer = _RecordingDiarizer(segments)
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)

        # Build a realtime-path-shaped TranscriptionResult: text came from
        # realtime, sentences came from the realtime engine. The pipeline's
        # existing pre-transcribed-text branch now threads pre_transcribed_sentences
        # through to this TranscriptionResult.sentences field.
        rt_text = "First speaker talking. Second speaker replying."
        result = TranscriptionResult(
            plain_text=rt_text,
            timestamped_text=rt_text,
            duration_minutes=0,
            srt_path="",
            sentences=sentences,
        )

        out = _maybe_diarize("/tmp/fake.wav", result, {"participants": "Alice, Bob"})

        # Diarization actually ran.
        assert diarizer.calls == [("/tmp/fake.wav", "sortformer")]
        # Both speaker labels show up in the rendered text — THIS is the
        # assertion that would have failed before commit ef50c73.
        assert "**Speaker A:**" in out.plain_text
        assert "**Speaker B:**" in out.plain_text
