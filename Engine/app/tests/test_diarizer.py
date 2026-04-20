"""Tests for app.diarizer — Protocol surface, FakeDiarizer, factory gating."""
from __future__ import annotations

import wave

import pytest

from app.diarizer import (
    DIARIZER_ENV_VAR,
    Diarizer,
    FakeDiarizer,
    get_diarizer,
)
from app.speaker_alignment import SpeakerSegment


def _write_silent_wav(path, duration_secs: float, rate: int = 16000) -> None:
    """Write a silent mono 16-bit PCM WAV of the requested duration."""
    n_frames = max(0, int(duration_secs * rate))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"\x00\x00" * n_frames)


class TestFakeDiarizerBasics:
    def test_protocol_conformance(self):
        # runtime_checkable Protocol — FakeDiarizer must satisfy it.
        assert isinstance(FakeDiarizer(), Diarizer)

    def test_rejects_bad_period(self):
        with pytest.raises(ValueError):
            FakeDiarizer(period_secs=0)
        with pytest.raises(ValueError):
            FakeDiarizer(period_secs=-1.0)

    def test_rejects_empty_speakers(self):
        with pytest.raises(ValueError):
            FakeDiarizer(speakers=())


class TestFakeDiarizerOutput:
    def test_segments_cover_full_duration(self, tmp_path):
        wav = tmp_path / "silent.wav"
        _write_silent_wav(wav, 45.0)
        out = FakeDiarizer(period_secs=15.0).diarize(str(wav))

        # 45s / 15s = 3 chunks, each exactly 15s long.
        assert len(out) == 3
        assert [s.end - s.start for s in out] == pytest.approx([15.0, 15.0, 15.0])
        # Contiguous coverage from 0 to duration.
        assert out[0].start == 0.0
        assert out[-1].end == pytest.approx(45.0)
        for a, b in zip(out, out[1:]):
            assert a.end == b.start

    def test_speakers_alternate(self, tmp_path):
        wav = tmp_path / "silent.wav"
        _write_silent_wav(wav, 30.0)
        out = FakeDiarizer(period_secs=10.0, speakers=("A", "B")).diarize(str(wav))
        assert [s.speaker for s in out] == ["A", "B", "A"]

    def test_partial_last_segment_capped_at_duration(self, tmp_path):
        wav = tmp_path / "silent.wav"
        _write_silent_wav(wav, 17.0)
        out = FakeDiarizer(period_secs=10.0).diarize(str(wav))
        # Two segments: 0-10 and 10-17 (capped, not 10-20).
        assert len(out) == 2
        assert out[-1].end == pytest.approx(17.0)

    def test_empty_audio_returns_empty_list(self, tmp_path):
        wav = tmp_path / "empty.wav"
        _write_silent_wav(wav, 0.0)
        assert FakeDiarizer().diarize(str(wav)) == []

    def test_missing_file_returns_empty_list_not_raise(self):
        # Diarizer failures must be swallowed into empty output — a broken
        # backend should never take down the whole pipeline.
        assert FakeDiarizer().diarize("/nonexistent/path.wav") == []

    def test_output_contains_only_speakersegments(self, tmp_path):
        wav = tmp_path / "silent.wav"
        _write_silent_wav(wav, 30.0)
        out = FakeDiarizer(period_secs=10.0).diarize(str(wav))
        assert all(isinstance(s, SpeakerSegment) for s in out)


class TestGetDiarizerFactory:
    def test_unset_env_returns_none(self, monkeypatch):
        monkeypatch.delenv(DIARIZER_ENV_VAR, raising=False)
        assert get_diarizer() is None

    def test_empty_env_returns_none(self, monkeypatch):
        monkeypatch.setenv(DIARIZER_ENV_VAR, "")
        assert get_diarizer() is None

    def test_fake_env_returns_fakediarizer(self, monkeypatch):
        monkeypatch.setenv(DIARIZER_ENV_VAR, "fake")
        d = get_diarizer()
        assert isinstance(d, FakeDiarizer)

    def test_fake_env_case_insensitive(self, monkeypatch):
        monkeypatch.setenv(DIARIZER_ENV_VAR, "FAKE")
        assert isinstance(get_diarizer(), FakeDiarizer)

    def test_unknown_backend_returns_none_and_warns(self, monkeypatch, caplog):
        monkeypatch.setenv(DIARIZER_ENV_VAR, "does-not-exist")
        with caplog.at_level("WARNING"):
            result = get_diarizer()
        assert result is None
        assert any("does-not-exist" in rec.message for rec in caplog.records)
