"""Tests for environment.py — paths and setup precondition checks."""
from __future__ import annotations

import os
import stat

import pytest


class TestLayout:
    def test_paths_descend_from_home_dir(self):
        """Every path constant should be rooted at HOME_DIR — sanity check
        that nothing leaked an absolute path during the refactor."""
        from app import environment as env
        assert env.ENGINE_DIR.startswith(env.HOME_DIR)
        assert env.BIN_DIR.startswith(env.ENGINE_DIR)
        assert env.QUEUE_DIR.startswith(env.ENGINE_DIR)
        assert env.ACTIVE_DIR.startswith(env.ENGINE_DIR)
        assert env.DONE_DIR.startswith(env.ENGINE_DIR)
        assert env.TRANSCRIPTS_DIR.startswith(env.HOME_DIR)
        assert env.SETTINGS_DIR.startswith(env.HOME_DIR)
        assert env.CONTEXT_PATH.startswith(env.SETTINGS_DIR)
        assert env.CAPTURE_AUDIO_BIN.startswith(env.BIN_DIR)
        assert env.SPEECH_TRANSCRIBE_BIN.startswith(env.BIN_DIR)

    def test_base_dir_is_home_dir_alias(self):
        """BASE_DIR is kept as an alias — a few external callers still
        use the older name. The test is here so a future cleanup that
        deletes BASE_DIR can intentionally remove this assertion too."""
        from app import environment as env
        assert env.BASE_DIR == env.HOME_DIR


class TestCheckSetup:
    @pytest.fixture
    def isolated_home(self, tmp_path, monkeypatch):
        """Point HOME_DIR + ENGINE_DIR at a temp tree and reload the module
        so derived constants pick up the change."""
        import importlib
        from app import environment as env
        monkeypatch.setenv("MEETINGNOTES_HOME", str(tmp_path))
        importlib.reload(env)
        try:
            yield tmp_path, env
        finally:
            # Restore the real HOME_DIR for subsequent tests.
            monkeypatch.delenv("MEETINGNOTES_HOME", raising=False)
            importlib.reload(env)

    def test_complete_setup_returns_empty(self, isolated_home):
        tmp, env = isolated_home
        # Build a fake Engine dir + executable capture-audio.
        os.makedirs(env.BIN_DIR, exist_ok=True)
        bin_path = env.CAPTURE_AUDIO_BIN
        with open(bin_path, "w") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(bin_path, stat.S_IRWXU)

        problems = env.check_setup()
        assert problems == []

    def test_missing_engine_dir_short_circuits(self, isolated_home):
        _, env = isolated_home
        # ENGINE_DIR doesn't exist (tmp_path is empty).
        problems = env.check_setup()
        assert len(problems) == 1
        assert "Engine directory missing" in problems[0]

    def test_missing_capture_audio_binary(self, isolated_home):
        tmp, env = isolated_home
        os.makedirs(env.ENGINE_DIR, exist_ok=True)
        # No binary at CAPTURE_AUDIO_BIN.

        problems = env.check_setup()
        assert len(problems) == 1
        assert "capture-audio" in problems[0]

    def test_non_executable_capture_audio_flagged(self, isolated_home):
        tmp, env = isolated_home
        os.makedirs(env.BIN_DIR, exist_ok=True)
        bin_path = env.CAPTURE_AUDIO_BIN
        with open(bin_path, "w") as f:
            f.write("not executable")
        os.chmod(bin_path, stat.S_IRUSR)  # readable but not executable.

        problems = env.check_setup()
        assert len(problems) == 1
        assert "capture-audio" in problems[0]
