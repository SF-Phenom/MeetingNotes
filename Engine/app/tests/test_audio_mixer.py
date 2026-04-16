"""Tests for audio_mixer — saturating mix of mic + system WAVs.

These tests generate tiny synthetic 16 kHz mono Int16 WAVs in tmp_path so
we don't depend on any real recording. The benchmark fixtures (if present)
exercise mix_to via test_pipeline_benchmark.py; this file covers unit logic.
"""
from __future__ import annotations

import struct
import wave

import pytest

from app.audio_mixer import mix_to, system_path_for


SAMPLE_RATE = 16000


def _write_wav(path, samples, sample_rate=SAMPLE_RATE):
    """Write a mono Int16 PCM WAV from a list of int samples."""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _read_wav_samples(path):
    """Read a mono Int16 WAV, return (samples, framerate)."""
    with wave.open(str(path), "rb") as wf:
        pcm = wf.readframes(wf.getnframes())
        return (
            list(struct.unpack(f"<{len(pcm)//2}h", pcm)),
            wf.getframerate(),
        )


class TestSystemPathFor:
    def test_appends_sys_suffix(self):
        assert system_path_for("zoom_2026-03-31_10-02.wav") == "zoom_2026-03-31_10-02.sys.wav"

    def test_preserves_directory(self):
        assert (
            system_path_for("/path/to/meet_2026-01-01_09-00.wav")
            == "/path/to/meet_2026-01-01_09-00.sys.wav"
        )


class TestMixTo:
    def test_missing_mic_returns_false(self, tmp_path):
        out = tmp_path / "out.wav"
        assert mix_to(str(tmp_path / "does-not-exist.wav"), "ignored", str(out)) is False
        assert not out.exists()

    def test_missing_system_writes_mic_only(self, tmp_path):
        mic = tmp_path / "mic.wav"
        out = tmp_path / "out.wav"
        _write_wav(mic, [100, 200, 300])

        assert mix_to(str(mic), str(tmp_path / "no-sys.wav"), str(out)) is True

        samples, sr = _read_wav_samples(out)
        assert samples == [100, 200, 300]
        assert sr == SAMPLE_RATE

    def test_simple_sum(self, tmp_path):
        mic = tmp_path / "mic.wav"
        sys = tmp_path / "mic.sys.wav"
        out = tmp_path / "out.wav"
        _write_wav(mic, [100, 200, 300])
        _write_wav(sys, [50, 75, 100])

        assert mix_to(str(mic), str(sys), str(out)) is True

        samples, _ = _read_wav_samples(out)
        assert samples == [150, 275, 400]

    def test_saturating_clip_positive(self, tmp_path):
        mic = tmp_path / "mic.wav"
        sys = tmp_path / "mic.sys.wav"
        out = tmp_path / "out.wav"
        _write_wav(mic, [20000, 20000])
        _write_wav(sys, [20000, 20000])

        assert mix_to(str(mic), str(sys), str(out)) is True

        samples, _ = _read_wav_samples(out)
        # 40000 would wrap Int16; saturation clips to 32767.
        assert samples == [32767, 32767]

    def test_saturating_clip_negative(self, tmp_path):
        mic = tmp_path / "mic.wav"
        sys = tmp_path / "mic.sys.wav"
        out = tmp_path / "out.wav"
        _write_wav(mic, [-20000, -20000])
        _write_wav(sys, [-20000, -20000])

        assert mix_to(str(mic), str(sys), str(out)) is True

        samples, _ = _read_wav_samples(out)
        assert samples == [-32768, -32768]

    def test_mismatched_lengths_pad_with_silence(self, tmp_path):
        mic = tmp_path / "mic.wav"
        sys = tmp_path / "mic.sys.wav"
        out = tmp_path / "out.wav"
        _write_wav(mic, [100, 200, 300, 400, 500])
        _write_wav(sys, [10, 20])  # shorter

        assert mix_to(str(mic), str(sys), str(out)) is True

        samples, _ = _read_wav_samples(out)
        # First two samples sum, remainder of mic passes through (sys is zero).
        assert samples == [110, 220, 300, 400, 500]

    def test_format_mismatch_falls_back_to_mic(self, tmp_path):
        mic = tmp_path / "mic.wav"
        sys = tmp_path / "mic.sys.wav"
        out = tmp_path / "out.wav"
        _write_wav(mic, [100, 200], sample_rate=16000)
        _write_wav(sys, [50, 75], sample_rate=48000)  # wrong sample rate

        assert mix_to(str(mic), str(sys), str(out)) is True

        samples, sr = _read_wav_samples(out)
        assert samples == [100, 200]
        assert sr == 16000

    def test_empty_inputs(self, tmp_path):
        mic = tmp_path / "mic.wav"
        sys = tmp_path / "mic.sys.wav"
        out = tmp_path / "out.wav"
        _write_wav(mic, [])
        _write_wav(sys, [])

        assert mix_to(str(mic), str(sys), str(out)) is True
        samples, _ = _read_wav_samples(out)
        assert samples == []

    @pytest.mark.parametrize(
        "mic_samples,sys_samples,expected",
        [
            ([0], [0], [0]),
            ([1, -1], [1, -1], [2, -2]),
            ([32767], [1], [32767]),   # +clip
            ([-32768], [-1], [-32768]),  # -clip
        ],
    )
    def test_param_sums(self, tmp_path, mic_samples, sys_samples, expected):
        mic = tmp_path / "mic.wav"
        sys = tmp_path / "mic.sys.wav"
        out = tmp_path / "out.wav"
        _write_wav(mic, mic_samples)
        _write_wav(sys, sys_samples)
        assert mix_to(str(mic), str(sys), str(out)) is True
        assert _read_wav_samples(out)[0] == expected
