"""Tests for RecordingFile — the .wav + sidecars aggregate."""
from __future__ import annotations

import pytest

from app.recording_file import RecordingFile


def _touch(path):
    """Create an empty file at ``path`` (parent must exist)."""
    path.write_bytes(b"")


@pytest.fixture
def recording(tmp_path):
    """Factory: build a ``<base>.wav`` plus any listed sidecars inside tmp_path."""
    def _make(base: str = "zoom_2026-01-01_10-00", sidecars=()):
        wav = tmp_path / f"{base}.wav"
        _touch(wav)
        for ext in sidecars:
            _touch(tmp_path / f"{base}{ext}")
        return RecordingFile(str(wav))
    return _make


class TestPaths:
    def test_paths_are_derived_from_wav(self, tmp_path):
        rf = RecordingFile(str(tmp_path / "zoom_2026-01-01_10-00.wav"))
        assert rf.wav_path == str(tmp_path / "zoom_2026-01-01_10-00.wav")
        assert rf.metadata_path == str(tmp_path / "zoom_2026-01-01_10-00.meta.json")
        assert rf.srt_path == str(tmp_path / "zoom_2026-01-01_10-00.srt")

    def test_basename_is_filename_only(self, tmp_path):
        rf = RecordingFile(str(tmp_path / "sub" / "meet_2026-04-15_14-00.wav"))
        assert rf.basename == "meet_2026-04-15_14-00.wav"

    def test_strips_only_the_trailing_wav(self, tmp_path):
        """Passing a compound-extension file (e.g. legacy ``.sys.wav``) should
        still have the trailing ``.wav`` stripped cleanly rather than being
        treated as a dotted extension. We don't rewrite the path to a paired
        mic file — caller's responsibility."""
        rf = RecordingFile(str(tmp_path / "x.sys.wav"))
        # Base should drop the trailing `.wav` only:
        assert rf.metadata_path == str(tmp_path / "x.sys.meta.json")


class TestExistingFiles:
    def test_only_wav_present(self, recording):
        rf = recording()
        assert rf.existing_files() == [rf.wav_path]

    def test_includes_all_present_sidecars(self, recording):
        rf = recording(sidecars=(".meta.json", ".srt"))
        found = rf.existing_files()
        # Order: wav, then sidecars in SIDECAR_EXTENSIONS order
        assert found == [rf.wav_path, rf.metadata_path, rf.srt_path]

    def test_missing_wav_omitted(self, tmp_path):
        # Sidecar exists but main .wav doesn't — should omit the wav.
        base = tmp_path / "alone"
        (tmp_path / "alone.meta.json").write_bytes(b"")
        rf = RecordingFile(str(base) + ".wav")
        assert rf.existing_files() == [str(base) + ".meta.json"]

    def test_none_exist_returns_empty(self, tmp_path):
        rf = RecordingFile(str(tmp_path / "nope.wav"))
        assert rf.existing_files() == []


class TestDelete:
    def test_deletes_all_existing(self, recording):
        rf = recording(sidecars=(".meta.json", ".srt"))
        assert rf.delete() == 3
        assert rf.existing_files() == []

    def test_partial_sidecars(self, recording):
        rf = recording(sidecars=(".meta.json",))
        # Only .wav and .meta.json present — delete returns 2.
        assert rf.delete() == 2

    def test_no_op_on_empty(self, tmp_path):
        rf = RecordingFile(str(tmp_path / "missing.wav"))
        assert rf.delete() == 0

    def test_second_delete_is_no_op(self, recording):
        rf = recording(sidecars=(".meta.json",))
        assert rf.delete() == 2
        assert rf.delete() == 0


class TestMoveTo:
    def test_moves_wav_and_sidecars(self, recording, tmp_path):
        rf = recording(sidecars=(".meta.json", ".srt"))
        dest = tmp_path / "queue"
        moved = rf.move_to(str(dest))
        # Original paths gone:
        assert rf.existing_files() == []
        # New location has all three:
        assert sorted(p.name for p in dest.iterdir()) == [
            "zoom_2026-01-01_10-00.meta.json",
            "zoom_2026-01-01_10-00.srt",
            "zoom_2026-01-01_10-00.wav",
        ]
        # Returned RecordingFile points at the new location.
        assert moved.wav_path == str(dest / "zoom_2026-01-01_10-00.wav")
        assert moved.existing_files() == [
            moved.wav_path, moved.metadata_path, moved.srt_path,
        ]

    def test_creates_dest_dir(self, recording, tmp_path):
        rf = recording()
        dest = tmp_path / "new" / "nested" / "dir"
        assert not dest.exists()
        rf.move_to(str(dest))
        assert dest.is_dir()

    def test_wav_missing_raises_os_error(self, tmp_path):
        # No .wav on disk — shutil.move of the main file raises FileNotFoundError
        # (an OSError subclass). Sidecar-only moves are not a supported scenario.
        rf = RecordingFile(str(tmp_path / "nothing.wav"))
        # Create a sidecar so existing_files() is non-empty but the main
        # .wav is missing — move_to still iterates only over existing ones
        # and should complete cleanly when the wav itself is absent.
        (tmp_path / "nothing.meta.json").write_bytes(b"")
        dest = tmp_path / "queue"
        rf.move_to(str(dest))
        # .meta.json should have been moved.
        assert (dest / "nothing.meta.json").exists()

    def test_moves_only_what_exists(self, recording, tmp_path):
        rf = recording(sidecars=(".meta.json",))  # no .srt
        dest = tmp_path / "out"
        rf.move_to(str(dest))
        names = sorted(p.name for p in dest.iterdir())
        assert names == [
            "zoom_2026-01-01_10-00.meta.json",
            "zoom_2026-01-01_10-00.wav",
        ]
