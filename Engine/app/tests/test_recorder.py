"""Tests for recorder.py — focused on the orphaned-recording recovery path.

The subprocess-launching parts of recorder.py (start_recording, stop_recording)
are not exercised here — they require the Swift capture-audio binary and a
real Process Tap. The WAV-header repair helper is pure-Python byte munging,
so it's covered thoroughly.
"""
from __future__ import annotations

import os
import struct
import wave

import pytest

from app import recorder


def _write_valid_pcm_wav(path: str, seconds: float = 0.1) -> int:
    """Write a small valid mono/16kHz/int16 WAV. Returns its byte size."""
    sample_rate = 16000
    n_samples = int(seconds * sample_rate)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_samples)
    return os.path.getsize(path)


def _zero_size_fields(path: str) -> None:
    """Simulate a SIGKILLed Swift capture: zero out RIFF + data size."""
    with open(path, "r+b") as f:
        f.seek(4)
        f.write(b"\x00\x00\x00\x00")
        f.seek(40)
        f.write(b"\x00\x00\x00\x00")


def _read_sizes(path: str) -> tuple[int, int]:
    """Return (riff_size, data_size) from the standard header offsets."""
    with open(path, "rb") as f:
        head = f.read(44)
    riff = struct.unpack("<I", head[4:8])[0]
    data = struct.unpack("<I", head[40:44])[0]
    return riff, data


class TestRepairTruncatedWavHeader:
    def test_repairs_the_exact_signature(self, tmp_path):
        """The regression case: Swift binary SIGKILLed, both size fields
        left at zero but PCM data intact. Repair patches them from the
        actual file size so QuickLook / Parakeet can read the file."""
        wav = tmp_path / "broken.wav"
        file_size = _write_valid_pcm_wav(str(wav))
        _zero_size_fields(str(wav))
        # Precondition: broken.
        assert _read_sizes(str(wav)) == (0, 0)

        repaired = recorder._repair_truncated_wav_header(str(wav))
        assert repaired is True
        assert _read_sizes(str(wav)) == (file_size - 8, file_size - 44)

    def test_leaves_valid_wav_untouched(self, tmp_path):
        """An already-well-formed WAV must not be rewritten. This is
        important for the tmp*.wav chunks that Python's wave module
        finalizes correctly — we'd corrupt them if we always patched."""
        wav = tmp_path / "good.wav"
        _write_valid_pcm_wav(str(wav))
        before = wav.read_bytes()

        repaired = recorder._repair_truncated_wav_header(str(wav))
        assert repaired is False
        assert wav.read_bytes() == before

    def test_skips_only_half_broken_headers(self, tmp_path):
        """If only one of the two size fields is zero, we don't know
        what kind of corruption we're looking at — refuse to guess."""
        wav = tmp_path / "half.wav"
        _write_valid_pcm_wav(str(wav))
        # Zero the data size but leave RIFF intact.
        with open(str(wav), "r+b") as f:
            f.seek(40)
            f.write(b"\x00\x00\x00\x00")
        before = wav.read_bytes()

        repaired = recorder._repair_truncated_wav_header(str(wav))
        assert repaired is False
        assert wav.read_bytes() == before

    def test_skips_file_smaller_than_header(self, tmp_path):
        """Runt files can't be PCM WAVs; leave them alone so we don't
        scribble garbage past EOF."""
        stub = tmp_path / "stub.wav"
        stub.write_bytes(b"RIFF")
        assert recorder._repair_truncated_wav_header(str(stub)) is False
        # Unchanged on disk.
        assert stub.read_bytes() == b"RIFF"

    def test_skips_non_riff_file(self, tmp_path):
        """Anything that doesn't start with a standard RIFF/WAVE/data
        layout gets left alone — we only repair the specific signature
        the Swift capture binary writes."""
        odd = tmp_path / "weird.wav"
        # 44 bytes of not-RIFF
        odd.write_bytes(b"OGGS" + b"\x00" * 40)
        before = odd.read_bytes()

        assert recorder._repair_truncated_wav_header(str(odd)) is False
        assert odd.read_bytes() == before

    def test_missing_file_returns_false(self, tmp_path):
        """The repair helper is called from a recovery loop — a missing
        file shouldn't throw and abort the whole recovery."""
        ghost = tmp_path / "nope.wav"
        assert recorder._repair_truncated_wav_header(str(ghost)) is False
