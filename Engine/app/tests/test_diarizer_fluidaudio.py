"""Tests for app.diarizer_fluidaudio — Python side of the Swift CLI.

We mock the subprocess so these tests run without the Swift binary
installed. The Swift code itself is exercised by building it (setup.command)
and by the real-WAV smoke test described in SETUP.md.
"""
from __future__ import annotations

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app import diarizer_fluidaudio as fa
from app.diarizer_fluidaudio import (
    DEFAULT_MODEL,
    FluidAudioDiarizer,
    _label_for_index,
    _relabel,
    _resolve_model,
    is_available,
)
from app.speaker_alignment import SpeakerSegment


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestLabelForIndex:
    def test_first_is_A(self):
        assert _label_for_index(0) == "Speaker A"

    def test_last_letter_is_Z(self):
        assert _label_for_index(25) == "Speaker Z"

    def test_beyond_26_uses_numbers(self):
        # "Speaker AA" would be unreadable in a transcript — we switch to
        # plain numbering past Z.
        assert _label_for_index(26) == "Speaker 27"
        assert _label_for_index(99) == "Speaker 100"


class TestResolveModel:
    def test_none_falls_back_to_default(self):
        assert _resolve_model(None) == DEFAULT_MODEL

    def test_known_models_pass_through(self):
        assert _resolve_model("community-1") == "community-1"
        assert _resolve_model("sortformer") == "sortformer"

    def test_unknown_model_warns_and_falls_back(self, caplog):
        with caplog.at_level("WARNING"):
            assert _resolve_model("diarizen-xl-v9") == DEFAULT_MODEL
        assert any("diarizen" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# Relabelling (raw IDs → Speaker A/B/C by first appearance)
# ---------------------------------------------------------------------------


class TestRelabel:
    def test_empty_list_returns_empty(self):
        assert _relabel([]) == []

    def test_single_speaker(self):
        raw = [{"start": 0.0, "end": 5.0, "speaker_id": "7"}]
        out = _relabel(raw)
        assert out == [SpeakerSegment(start=0.0, end=5.0, speaker="Speaker A")]

    def test_first_appearance_drives_letter_order(self):
        # Raw IDs intentionally weird ("7", "3") to prove we're labelling
        # by first-appearance *time*, not by sort order of the raw IDs.
        raw = [
            {"start": 10.0, "end": 20.0, "speaker_id": "7"},  # 1st speaker → A
            {"start": 0.0, "end": 5.0, "speaker_id": "3"},   # appears earlier → A
            {"start": 5.0, "end": 10.0, "speaker_id": "7"},  # 2nd speaker in time → B
        ]
        out = _relabel(raw)
        # Sorted by start, "3" appears first → Speaker A, "7" next → Speaker B.
        labels = [s.speaker for s in out]
        assert labels == ["Speaker A", "Speaker B", "Speaker B"]

    def test_malformed_segments_skipped(self):
        raw = [
            {"start": 0.0, "end": 5.0, "speaker_id": "1"},
            {"start": "bogus", "end": 5.0, "speaker_id": "1"},  # bad start
            {"start": 5.0, "end": 5.0, "speaker_id": "1"},     # zero duration
            {"start": 10.0, "end": 5.0, "speaker_id": "1"},    # end before start
            {"start": 15.0, "end": 20.0, "speaker_id": "2"},
        ]
        out = _relabel(raw)
        # Only the two well-formed segments survive.
        assert len(out) == 2
        assert out[0].speaker == "Speaker A"
        assert out[1].speaker == "Speaker B"

    def test_output_sorted_by_start(self):
        raw = [
            {"start": 30.0, "end": 40.0, "speaker_id": "1"},
            {"start": 10.0, "end": 20.0, "speaker_id": "2"},
            {"start": 0.0, "end": 5.0, "speaker_id": "3"},
        ]
        out = _relabel(raw)
        assert [s.start for s in out] == [0.0, 10.0, 30.0]


# ---------------------------------------------------------------------------
# is_available()
# ---------------------------------------------------------------------------


class TestIsAvailable:
    def test_missing_binary_returns_false(self, monkeypatch, tmp_path):
        monkeypatch.setattr(fa, "DIARIZE_BIN", str(tmp_path / "nonexistent"))
        assert is_available() is False

    def test_existing_nonexec_returns_false(self, monkeypatch, tmp_path):
        p = tmp_path / "diarize"
        p.write_text("#!/bin/sh\necho hi\n")
        # No chmod +x → not executable.
        monkeypatch.setattr(fa, "DIARIZE_BIN", str(p))
        assert is_available() is False

    def test_executable_returns_true(self, monkeypatch, tmp_path):
        p = tmp_path / "diarize"
        p.write_text("#!/bin/sh\necho hi\n")
        p.chmod(0o755)
        monkeypatch.setattr(fa, "DIARIZE_BIN", str(p))
        assert is_available() is True


# ---------------------------------------------------------------------------
# FluidAudioDiarizer.diarize() — subprocess paths, all mocked
# ---------------------------------------------------------------------------


@pytest.fixture
def stub_binary(monkeypatch, tmp_path):
    """Point DIARIZE_BIN at a real executable so is_available() passes.

    The binary is never actually invoked — subprocess.run is patched.
    """
    p = tmp_path / "diarize"
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(0o755)
    monkeypatch.setattr(fa, "DIARIZE_BIN", str(p))
    return str(p)


def _make_completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stderr=stderr, stdout="")


class TestDiarizerNoBinary:
    def test_missing_binary_returns_none_and_warns(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(fa, "DIARIZE_BIN", str(tmp_path / "nope"))
        with caplog.at_level("WARNING"):
            out = FluidAudioDiarizer().diarize("/tmp/audio.wav")
        assert out is None
        assert any("binary missing" in rec.message for rec in caplog.records)


class TestDiarizerSubprocessPaths:
    def _patch_subprocess_success(self, monkeypatch, tmp_path, segments_payload):
        """Make subprocess.run succeed and drop segments_payload at --output."""
        def fake_run(cmd, capture_output, text, timeout):
            # Extract --output path from argv
            out_idx = cmd.index("--output") + 1
            with open(cmd[out_idx], "w") as f:
                json.dump({"segments": segments_payload}, f)
            return _make_completed()
        monkeypatch.setattr(subprocess, "run", fake_run)

    def test_happy_path_returns_labelled_segments(
        self, stub_binary, monkeypatch, tmp_path,
    ):
        self._patch_subprocess_success(
            monkeypatch, tmp_path,
            [
                {"start": 0.0, "end": 5.0, "speaker_id": "0"},
                {"start": 5.0, "end": 10.0, "speaker_id": "1"},
            ],
        )
        out = FluidAudioDiarizer().diarize("/tmp/audio.wav")
        assert out is not None
        assert [s.speaker for s in out] == ["Speaker A", "Speaker B"]
        assert out[0].start == 0.0 and out[0].end == 5.0

    def test_output_file_is_cleaned_up(self, stub_binary, monkeypatch, tmp_path):
        captured_paths: list[str] = []

        def fake_run(cmd, capture_output, text, timeout):
            out_idx = cmd.index("--output") + 1
            captured_paths.append(cmd[out_idx])
            with open(cmd[out_idx], "w") as f:
                json.dump({"segments": []}, f)
            return _make_completed()

        monkeypatch.setattr(subprocess, "run", fake_run)
        FluidAudioDiarizer().diarize("/tmp/audio.wav")
        # The tempfile path handed to Swift should be gone after diarize() returns.
        assert captured_paths
        assert not os.path.exists(captured_paths[0])

    def test_nonzero_exit_returns_none_and_logs_stderr_preview(
        self, stub_binary, monkeypatch, tmp_path, caplog,
    ):
        def fake_run(cmd, capture_output, text, timeout):
            return _make_completed(returncode=1, stderr="model download failed\n")
        monkeypatch.setattr(subprocess, "run", fake_run)

        with caplog.at_level("WARNING"):
            out = FluidAudioDiarizer().diarize("/tmp/audio.wav")
        assert out is None
        assert any("model download failed" in rec.message for rec in caplog.records)

    def test_timeout_returns_none(self, stub_binary, monkeypatch):
        def fake_run(cmd, capture_output, text, timeout):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert FluidAudioDiarizer().diarize("/tmp/audio.wav") is None

    def test_malformed_json_returns_none(self, stub_binary, monkeypatch):
        def fake_run(cmd, capture_output, text, timeout):
            out_idx = cmd.index("--output") + 1
            with open(cmd[out_idx], "w") as f:
                f.write("{not valid json")
            return _make_completed()
        monkeypatch.setattr(subprocess, "run", fake_run)

        assert FluidAudioDiarizer().diarize("/tmp/audio.wav") is None


class TestDiarizerModelFlag:
    def _capture_args(self, monkeypatch):
        seen: list[list[str]] = []
        def fake_run(cmd, capture_output, text, timeout):
            seen.append(list(cmd))
            out_idx = cmd.index("--output") + 1
            with open(cmd[out_idx], "w") as f:
                json.dump({"segments": []}, f)
            return _make_completed()
        monkeypatch.setattr(subprocess, "run", fake_run)
        return seen

    def test_default_is_community_1(self, stub_binary, monkeypatch):
        seen = self._capture_args(monkeypatch)
        FluidAudioDiarizer().diarize("/tmp/audio.wav")
        assert "--model" in seen[0]
        assert seen[0][seen[0].index("--model") + 1] == "community-1"

    def test_sortformer_flag_is_forwarded(self, stub_binary, monkeypatch):
        seen = self._capture_args(monkeypatch)
        FluidAudioDiarizer().diarize("/tmp/audio.wav", model="sortformer")
        assert seen[0][seen[0].index("--model") + 1] == "sortformer"

    def test_unknown_model_falls_back_to_default(self, stub_binary, monkeypatch):
        seen = self._capture_args(monkeypatch)
        FluidAudioDiarizer().diarize("/tmp/audio.wav", model="not-a-real-model")
        assert seen[0][seen[0].index("--model") + 1] == DEFAULT_MODEL


class TestDiarizerSpeakerBounds:
    """Passthrough tests for --min-speakers / --max-speakers CLI flags."""

    def _capture_args(self, monkeypatch):
        seen: list[list[str]] = []
        def fake_run(cmd, capture_output, text, timeout):
            seen.append(list(cmd))
            out_idx = cmd.index("--output") + 1
            with open(cmd[out_idx], "w") as f:
                json.dump({"segments": []}, f)
            return _make_completed()
        monkeypatch.setattr(subprocess, "run", fake_run)
        return seen

    def test_bounds_omitted_by_default(self, stub_binary, monkeypatch):
        # Callers that don't know the speaker count must not force either
        # bound — FluidAudio's community defaults stay in charge.
        seen = self._capture_args(monkeypatch)
        FluidAudioDiarizer().diarize("/tmp/audio.wav")
        assert "--min-speakers" not in seen[0]
        assert "--max-speakers" not in seen[0]

    def test_min_speakers_forwarded(self, stub_binary, monkeypatch):
        seen = self._capture_args(monkeypatch)
        FluidAudioDiarizer().diarize("/tmp/audio.wav", min_speakers=2)
        argv = seen[0]
        assert argv[argv.index("--min-speakers") + 1] == "2"
        assert "--max-speakers" not in argv

    def test_max_speakers_forwarded(self, stub_binary, monkeypatch):
        seen = self._capture_args(monkeypatch)
        FluidAudioDiarizer().diarize("/tmp/audio.wav", max_speakers=5)
        argv = seen[0]
        assert argv[argv.index("--max-speakers") + 1] == "5"
        assert "--min-speakers" not in argv

    def test_both_bounds_forwarded(self, stub_binary, monkeypatch):
        seen = self._capture_args(monkeypatch)
        FluidAudioDiarizer().diarize(
            "/tmp/audio.wav", model="community-1", min_speakers=2, max_speakers=8,
        )
        argv = seen[0]
        assert argv[argv.index("--min-speakers") + 1] == "2"
        assert argv[argv.index("--max-speakers") + 1] == "8"
        assert argv[argv.index("--model") + 1] == "community-1"

    def test_explicit_none_omits_flags(self, stub_binary, monkeypatch):
        # Defensive: callers that pass None explicitly (e.g. pipeline when
        # it has no count hint) get the same subprocess argv as callers who
        # omit the kwargs entirely.
        seen = self._capture_args(monkeypatch)
        FluidAudioDiarizer().diarize(
            "/tmp/audio.wav", min_speakers=None, max_speakers=None,
        )
        assert "--min-speakers" not in seen[0]
        assert "--max-speakers" not in seen[0]
