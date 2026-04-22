"""Tests for the pipeline-level diarization step.

Covers the two call paths and the model-routing decision. The critical
test here is ``test_realtime_shortcut_path_diarizes`` — exercising the
scenario that silently broke under the batch-engine-wiring design: the
pipeline taking its pre-transcribed-text fast path from realtime and
never invoking diarization. The previous commit's tests all passed but
none covered this path, which is exactly why the bug shipped.
"""
from __future__ import annotations

import json
import os

import pytest

from app import pipeline
from app.pipeline import (
    DIARIZATION_ENABLED_ENV_VAR,
    _maybe_diarize,
    _pick_diarizer,
    _read_participants_sidecar,
)
from app.speaker_alignment import SpeakerSegment
from app.transcript_formatter import Sentence
from app.transcriber import TranscriptionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wav_path_for(tmp_path, name: str = "zoom_2026-04-22_10-00.wav") -> str:
    """Return a path inside tmp_path. The file need not exist — _pick_diarizer
    only uses the path to derive the sidecar name."""
    return str(tmp_path / name)


def _write_sidecar(tmp_path, wav_path: str, records: list[dict]) -> None:
    """Write a .participants.jsonl sidecar next to `wav_path`."""
    base = wav_path[:-4] if wav_path.endswith(".wav") else os.path.splitext(wav_path)[0]
    sidecar = base + ".participants.jsonl"
    with open(sidecar, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


# ---------------------------------------------------------------------------
# _read_participants_sidecar — JSONL parsing
# ---------------------------------------------------------------------------


class TestReadParticipantsSidecar:
    def test_missing_sidecar_returns_none(self, tmp_path):
        assert _read_participants_sidecar(_wav_path_for(tmp_path)) is None

    def test_empty_file_returns_none(self, tmp_path):
        wav = _wav_path_for(tmp_path)
        _write_sidecar(tmp_path, wav, [])
        assert _read_participants_sidecar(wav) is None

    def test_all_null_counts_returns_none(self, tmp_path):
        # Observer ran, panel never opened — well-formed file but no
        # usable signal. Must return None, not 0, so the pipeline falls
        # back to calendar rather than treating it as "zero people".
        wav = _wav_path_for(tmp_path)
        _write_sidecar(tmp_path, wav, [
            {"t": 0.0, "count": None, "reason": "panel_closed", "observer_version": 1},
            {"t": 10.0, "count": None, "reason": "panel_closed", "observer_version": 1},
        ])
        assert _read_participants_sidecar(wav) is None

    def test_peak_of_mixed_records(self, tmp_path):
        wav = _wav_path_for(tmp_path)
        _write_sidecar(tmp_path, wav, [
            {"t": 0.0, "count": 2, "observer_version": 1},
            {"t": 10.0, "count": 4, "observer_version": 1},
            {"t": 20.0, "count": None, "reason": "panel_closed", "observer_version": 1},
            {"t": 30.0, "count": 3, "observer_version": 1},
        ])
        assert _read_participants_sidecar(wav) == 4

    def test_tolerates_partial_trailing_line(self, tmp_path):
        # SIGKILLed observer may leave the last line unclosed. The
        # truncated record itself is lost (no way to recover a valid
        # count from malformed JSON), but the parser must NOT raise —
        # earlier valid records still drive the peak.
        wav = _wav_path_for(tmp_path)
        base = wav[:-4]
        with open(base + ".participants.jsonl", "w") as f:
            f.write('{"t": 0.0, "count": 3, "observer_version": 1}\n')
            f.write('{"t": 10.0, "count": 5, "obs')  # truncated — skipped
        assert _read_participants_sidecar(wav) == 3

    def test_ignores_zero_counts(self, tmp_path):
        # Defensive: observer should never emit count: 0 (that's always
        # null instead), but if it ever did, we'd treat it as no signal.
        wav = _wav_path_for(tmp_path)
        _write_sidecar(tmp_path, wav, [
            {"t": 0.0, "count": 0, "observer_version": 1},
        ])
        assert _read_participants_sidecar(wav) is None

    def test_non_wav_path_also_works(self, tmp_path):
        # Edge case — base path gets derived by stripping trailing
        # extension, not strictly .wav. This matches RecordingFile's
        # tolerance for non-.wav inputs.
        path = str(tmp_path / "recording.audio")
        base = os.path.splitext(path)[0]
        with open(base + ".participants.jsonl", "w") as f:
            f.write('{"t": 0.0, "count": 2, "observer_version": 1}\n')
        assert _read_participants_sidecar(path) == 2


# ---------------------------------------------------------------------------
# _pick_diarizer — model + bounds derivation
# ---------------------------------------------------------------------------


class TestPickDiarizerCalendarOnly:
    """Legacy calendar-only path — AX sidecar absent everywhere."""

    def test_no_participants_defaults_to_sortformer_no_bounds(self, tmp_path):
        # Ad-hoc recording with no calendar match AND no AX observer:
        # sortformer, no hints.
        assert _pick_diarizer({}, _wav_path_for(tmp_path)) == ("sortformer", None, None)

    def test_empty_participants_string(self, tmp_path):
        assert _pick_diarizer(
            {"participants": ""}, _wav_path_for(tmp_path),
        ) == ("sortformer", None, None)
        assert _pick_diarizer(
            {"participants": "   "}, _wav_path_for(tmp_path),
        ) == ("sortformer", None, None)

    def test_non_string_participants(self, tmp_path):
        # Defensive — metadata might carry a list or None from some edge path.
        assert _pick_diarizer(
            {"participants": None}, _wav_path_for(tmp_path),
        ) == ("sortformer", None, None)
        assert _pick_diarizer(
            {"participants": ["Alice"]}, _wav_path_for(tmp_path),
        ) == ("sortformer", None, None)

    def test_single_other_attendee_picks_sortformer_with_bounds(self, tmp_path):
        # 1 other + user = 2 total → sortformer, min=2, max=4.
        assert _pick_diarizer(
            {"participants": "Alice"}, _wav_path_for(tmp_path),
        ) == ("sortformer", 2, 4)

    def test_six_total_still_picks_sortformer(self, tmp_path):
        # 5 others + user = 6 → right at the boundary, still sortformer.
        assert _pick_diarizer(
            {"participants": "A, B, C, D, E"}, _wav_path_for(tmp_path),
        ) == ("sortformer", 2, 8)

    def test_seven_total_falls_back_to_community_1(self, tmp_path):
        # 6 others + user = 7 → over the Sortformer cap → community-1.
        assert _pick_diarizer(
            {"participants": "A, B, C, D, E, F"}, _wav_path_for(tmp_path),
        ) == ("community-1", 2, 9)

    def test_large_meeting_is_community_1(self, tmp_path):
        many = ", ".join(f"Person{i}" for i in range(20))
        model, min_, max_ = _pick_diarizer(
            {"participants": many}, _wav_path_for(tmp_path),
        )
        assert model == "community-1"
        assert min_ == 2
        assert max_ == 23  # 20 others + user + 2 headroom

    def test_stray_whitespace_and_empty_slots_ignored(self, tmp_path):
        # Defensive parsing — stray commas and whitespace don't inflate
        # the count. 2 real names + user = 3 total.
        assert _pick_diarizer(
            {"participants": " Alice ,  , Bob ,"}, _wav_path_for(tmp_path),
        ) == ("sortformer", 2, 5)


class TestPickDiarizerAXOverride:
    """AX sidecar takes precedence over calendar when present."""

    def test_ax_peak_overrides_calendar(self, tmp_path):
        # Calendar says 3 total (2 others + user). AX saw 8 people
        # actually show up → AX peak wins, routing crosses into
        # community-1 territory.
        wav = _wav_path_for(tmp_path)
        _write_sidecar(tmp_path, wav, [
            {"t": 0.0, "count": 3, "observer_version": 1},
            {"t": 10.0, "count": 8, "observer_version": 1},
            {"t": 20.0, "count": 6, "observer_version": 1},
        ])
        model, min_, max_ = _pick_diarizer({"participants": "Alice, Bob"}, wav)
        assert model == "community-1"  # 8 > 6
        assert min_ == 2
        assert max_ == 10  # 8 + 2

    def test_ax_lower_than_calendar_also_wins(self, tmp_path):
        # Calendar says 10 people were invited. Only 3 actually joined.
        # AX signal wins, routes to sortformer with a tight ceiling.
        wav = _wav_path_for(tmp_path)
        _write_sidecar(tmp_path, wav, [
            {"t": 0.0, "count": 3, "observer_version": 1},
        ])
        many = ", ".join(f"P{i}" for i in range(9))  # 9 others + user = 10
        model, min_, max_ = _pick_diarizer({"participants": many}, wav)
        assert model == "sortformer"
        assert min_ == 2
        assert max_ == 5

    def test_ax_single_participant_omits_min_bound(self, tmp_path):
        # User alone in the Zoom (waiting, testing, whatever). AX
        # reports 1. max=3 gives headroom but min must stay None so
        # FluidAudio doesn't force a split of one voice into two.
        wav = _wav_path_for(tmp_path)
        _write_sidecar(tmp_path, wav, [
            {"t": 0.0, "count": 1, "observer_version": 1},
        ])
        model, min_, max_ = _pick_diarizer({}, wav)
        assert model == "sortformer"
        assert min_ is None
        assert max_ == 3


class TestPickDiarizerAXAbsentFallback:
    """When AX produces no usable signal, calendar takes over."""

    def test_null_only_sidecar_falls_back_to_calendar(self, tmp_path):
        # Observer ran, panel never opened. Same behavior as if no
        # sidecar existed: use calendar.
        wav = _wav_path_for(tmp_path)
        _write_sidecar(tmp_path, wav, [
            {"t": 0.0, "count": None, "reason": "panel_closed", "observer_version": 1},
            {"t": 10.0, "count": None, "reason": "panel_closed", "observer_version": 1},
        ])
        assert _pick_diarizer(
            {"participants": "A, B, C, D, E, F"}, wav,
        ) == ("community-1", 2, 9)

    def test_no_ax_no_calendar_returns_no_hint(self, tmp_path):
        # Ad-hoc recording with neither signal: today's sortformer default
        # and no bounds hint — lets FluidAudio run on its own.
        assert _pick_diarizer({}, _wav_path_for(tmp_path)) == ("sortformer", None, None)


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
        # (wav_path, model, min_speakers, max_speakers) — mirrors the
        # full Diarizer Protocol signature post-Commit-0 bounds wiring.
        self.calls: list[
            tuple[str, str | None, int | None, int | None]
        ] = []

    def diarize(
        self,
        wav_path: str,
        model: str | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ):
        self.calls.append((wav_path, model, min_speakers, max_speakers))
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
        # match → sortformer (the ad-hoc default). No AX sidecar either
        # (the path doesn't exist), so bounds are None/None.
        assert diarizer.calls == [("/tmp/x.wav", "sortformer", None, None)]

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
        # 2 others + user = 3 → sortformer, min=2, max=5.
        assert diarizer.calls[0][1:] == ("sortformer", 2, 5)

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
        # 6 others + user = 7 → community-1, min=2, max=9.
        assert diarizer.calls[0][1:] == ("community-1", 2, 9)

    def test_empty_segments_return_input_unchanged(
        self, diarization_on, monkeypatch,
    ):
        # Single-speaker recording: backend decides no labels are useful.
        diarizer = _RecordingDiarizer([])
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)
        result = _make_result([Sentence(0.0, 5.0, "hi")])
        out = _maybe_diarize("/tmp/x.wav", result, {})
        assert out is result

    def test_ax_sidecar_flows_bounds_into_diarizer_call(
        self, diarization_on, monkeypatch, tmp_path,
    ):
        # End-to-end wiring: sidecar next to the wav → _maybe_diarize
        # picks up bounds → diarizer receives min/max kwargs. Covers the
        # gap between the _pick_diarizer unit tests and the diarizer
        # subprocess passthrough tests in test_diarizer_fluidaudio.py.
        wav = tmp_path / "zoom_2026-04-22_10-00.wav"
        wav.write_bytes(b"")  # empty placeholder — diarizer is mocked
        _write_sidecar(tmp_path, str(wav), [
            {"t": 0.0, "count": 4, "observer_version": 1},
            {"t": 10.0, "count": 7, "observer_version": 1},
        ])
        diarizer = _RecordingDiarizer([
            SpeakerSegment(0.0, 5.0, "Speaker A"),
        ])
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: diarizer)
        result = _make_result([Sentence(0.0, 5.0, "hi")])
        # Calendar says 3 total. AX peaked at 7 → AX wins, routes
        # community-1 with max=9.
        _maybe_diarize(str(wav), result, {"participants": "Alice, Bob"})
        assert diarizer.calls[0] == (str(wav), "community-1", 2, 9)

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

        # Diarization actually ran. 2 others + user = 3 → sortformer, (2, 5).
        assert diarizer.calls == [("/tmp/fake.wav", "sortformer", 2, 5)]
        # Both speaker labels show up in the rendered text — THIS is the
        # assertion that would have failed before commit ef50c73.
        assert "**Speaker A:**" in out.plain_text
        assert "**Speaker B:**" in out.plain_text
