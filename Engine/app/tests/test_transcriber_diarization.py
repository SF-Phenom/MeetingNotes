"""Tests for the diarization gating helper in app.transcriber.

The _diarize_if_enabled helper is the one call site the batch pipeline
uses to decide whether to run speaker diarization. It must:

  * pass sentences through unchanged when the feature flag is off
  * pass them through when no backend is registered
  * swallow backend exceptions rather than failing transcription
  * actually run the diarizer + alignment layer when enabled + backend available
"""
from __future__ import annotations

import wave

import pytest

from app import state as state_mod
from app import transcriber
from app.diarizer import DIARIZER_ENV_VAR
from app.speaker_alignment import SpeakerSegment
from app.transcriber import DIARIZATION_ENABLED_ENV_VAR, _diarize_if_enabled
from app.transcript_formatter import Sentence


def _write_silent_wav(path, duration_secs: float, rate: int = 16000) -> None:
    n_frames = max(0, int(duration_secs * rate))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n_frames)


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    """Throwaway state.json so the flag resolver has something to read."""
    path = tmp_path / "state.json"
    monkeypatch.setattr(state_mod, "STATE_PATH", str(path))
    return path


@pytest.fixture
def silent_wav(tmp_path):
    wav = tmp_path / "audio.wav"
    _write_silent_wav(wav, 30.0)
    return str(wav)


def _sent(start: float, end: float, text: str = "hi") -> Sentence:
    return Sentence(start=start, end=end, text=text)


class TestEnvVarOverride:
    def test_env_off_short_circuits(self, silent_wav, monkeypatch, state_file):
        # Even if state says enabled, MEETINGNOTES_DIARIZATION=0 wins.
        state_mod.update(diarization_enabled=True)
        monkeypatch.setenv(DIARIZATION_ENABLED_ENV_VAR, "0")
        monkeypatch.setenv(DIARIZER_ENV_VAR, "fake")
        sents = [_sent(0.0, 10.0)]
        out = _diarize_if_enabled(silent_wav, sents)
        # Input is returned untouched (no speaker assigned).
        assert out is sents
        assert out[0].speaker is None

    def test_env_on_overrides_state(self, silent_wav, monkeypatch, state_file):
        # State says disabled, env flips it on — diarization runs.
        state_mod.update(diarization_enabled=False)
        monkeypatch.setenv(DIARIZATION_ENABLED_ENV_VAR, "1")
        monkeypatch.setenv(DIARIZER_ENV_VAR, "fake")
        sents = [_sent(0.0, 10.0)]
        out = _diarize_if_enabled(silent_wav, sents)
        assert out[0].speaker is not None

    def test_env_unset_falls_back_to_state(self, silent_wav, monkeypatch, state_file):
        monkeypatch.delenv(DIARIZATION_ENABLED_ENV_VAR, raising=False)
        monkeypatch.setenv(DIARIZER_ENV_VAR, "fake")
        state_mod.update(diarization_enabled=True)
        sents = [_sent(0.0, 10.0)]
        out = _diarize_if_enabled(silent_wav, sents)
        assert out[0].speaker is not None


class TestNoBackendPathways:
    def test_enabled_but_no_backend_returns_input(self, silent_wav, monkeypatch, state_file):
        monkeypatch.setenv(DIARIZATION_ENABLED_ENV_VAR, "1")
        monkeypatch.delenv(DIARIZER_ENV_VAR, raising=False)
        sents = [_sent(0.0, 10.0)]
        out = _diarize_if_enabled(silent_wav, sents)
        assert out is sents
        assert out[0].speaker is None

    def test_diarizer_exception_is_swallowed(self, silent_wav, monkeypatch, state_file):
        # A backend that raises must not crash the pipeline — a transcript
        # without speaker labels is strictly better than no transcript.
        class ExplodingDiarizer:
            def diarize(self, wav_path):
                raise RuntimeError("simulated backend failure")

        monkeypatch.setenv(DIARIZATION_ENABLED_ENV_VAR, "1")
        monkeypatch.setattr(
            "app.diarizer.get_diarizer", lambda: ExplodingDiarizer()
        )
        sents = [_sent(0.0, 10.0)]
        out = _diarize_if_enabled(silent_wav, sents)
        # Same list back (or same contents); no crash, no speaker assigned.
        assert len(out) == 1
        assert out[0].speaker is None


class TestEndToEndWithFake:
    def test_fake_diarizer_labels_sentences(self, silent_wav, monkeypatch, state_file):
        monkeypatch.setenv(DIARIZATION_ENABLED_ENV_VAR, "1")
        monkeypatch.setenv(DIARIZER_ENV_VAR, "fake")
        # 30s WAV, FakeDiarizer default period=15s → "Speaker A" for 0-15,
        # "Speaker B" for 15-30. A sentence at [0,10] falls in A's segment.
        sents = [_sent(0.0, 10.0, "first"), _sent(20.0, 25.0, "second")]
        out = _diarize_if_enabled(silent_wav, sents)
        assert [s.speaker for s in out] == ["Speaker A", "Speaker B"]

    def test_empty_segments_leaves_speakers_none(self, silent_wav, monkeypatch, state_file):
        # Backend returns [] — we treat that as "nothing to label," return input.
        class EmptyDiarizer:
            def diarize(self, wav_path):
                return []

        monkeypatch.setenv(DIARIZATION_ENABLED_ENV_VAR, "1")
        monkeypatch.setattr("app.diarizer.get_diarizer", lambda: EmptyDiarizer())
        sents = [_sent(0.0, 10.0)]
        out = _diarize_if_enabled(silent_wav, sents)
        assert out[0].speaker is None
