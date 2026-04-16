"""Tests for ModelManager — Ollama discovery, preferences, API key I/O.

These patch out summarizer's network/env calls so the tests are hermetic.
"""
from __future__ import annotations

import os

import pytest

from app import model_manager as mm
from app import summarizer


@pytest.fixture(autouse=True)
def _reset_preference():
    """Leave the summarizer preference in a clean state between tests."""
    before = summarizer.get_model_preference()
    yield
    summarizer.set_model_preference(before)


class TestDiscover:
    def test_successful_discovery(self, monkeypatch):
        monkeypatch.setattr(
            summarizer, "discover_ollama_models", lambda: ["qwen3:4b", "llama3.1:8b"],
        )
        m = mm.ModelManager()
        m.discover()
        assert m.available_models() == ["qwen3:4b", "llama3.1:8b"]

    def test_discovery_error_yields_empty_list(self, monkeypatch):
        def _boom():
            raise RuntimeError("ollama down")

        monkeypatch.setattr(summarizer, "discover_ollama_models", _boom)
        m = mm.ModelManager()
        m.discover()
        assert m.available_models() == []

    def test_available_models_returns_copy(self, monkeypatch):
        monkeypatch.setattr(summarizer, "discover_ollama_models", lambda: ["a"])
        m = mm.ModelManager()
        m.discover()
        got = m.available_models()
        got.append("mutated")
        assert m.available_models() == ["a"]  # internal list unchanged


class TestPreference:
    def test_current_matches_summarizer(self):
        summarizer.set_model_preference("claude")
        m = mm.ModelManager()
        assert m.current_preference() == "claude"

    def test_set_propagates_to_summarizer(self):
        m = mm.ModelManager()
        m.set_preference("qwen3:4b")
        assert summarizer.get_model_preference() == "qwen3:4b"


class TestApiKeyPresent:
    def test_env_unset(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert mm.ModelManager().api_key_present() is False

    def test_env_empty_string_is_absent(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "")
        assert mm.ModelManager().api_key_present() is False

    def test_env_set(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-deadbeef")
        assert mm.ModelManager().api_key_present() is True


class TestSaveApiKey:
    @pytest.fixture
    def home(self, tmp_path, monkeypatch):
        """Point ~/.zshrc at a temp file via HOME override."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        return tmp_path

    def test_empty_key_rejected(self, home):
        ok, _ = mm.ModelManager().save_api_key("")
        assert ok is False

    def test_whitespace_only_rejected(self, home):
        ok, _ = mm.ModelManager().save_api_key("    ")
        assert ok is False

    def test_wrong_prefix_rejected(self, home):
        ok, msg = mm.ModelManager().save_api_key("not-a-real-key")
        assert ok is False
        assert "sk-ant-" in msg

    def test_wrong_prefix_does_not_update_env(self, home):
        assert os.environ.get("ANTHROPIC_API_KEY") is None
        mm.ModelManager().save_api_key("nope")
        assert os.environ.get("ANTHROPIC_API_KEY") is None

    def test_valid_key_writes_zshrc_and_env(self, home):
        key = "sk-ant-test-" + "x" * 20
        ok, _ = mm.ModelManager().save_api_key(key)
        assert ok is True
        assert os.environ["ANTHROPIC_API_KEY"] == key
        zshrc = (home / ".zshrc").read_text()
        assert 'export ANTHROPIC_API_KEY="{}"'.format(key) in zshrc

    def test_key_strips_whitespace(self, home):
        key = "sk-ant-trimmed-" + "y" * 10
        ok, _ = mm.ModelManager().save_api_key("  " + key + "  \n")
        assert ok is True
        assert os.environ["ANTHROPIC_API_KEY"] == key

    def test_existing_zshrc_preserves_other_lines(self, home):
        zshrc = home / ".zshrc"
        zshrc.write_text(
            "# my config\n"
            "export PATH=/usr/local/bin:$PATH\n"
            'export OTHER_KEY="abc"\n'
        )
        key = "sk-ant-new-" + "z" * 20
        ok, _ = mm.ModelManager().save_api_key(key)
        assert ok is True
        contents = zshrc.read_text()
        assert "my config" in contents
        assert "export PATH=/usr/local/bin" in contents
        assert 'export OTHER_KEY="abc"' in contents
        assert 'ANTHROPIC_API_KEY="{}"'.format(key) in contents

    def test_replaces_existing_api_key_line(self, home):
        zshrc = home / ".zshrc"
        zshrc.write_text(
            '# before\n'
            'export ANTHROPIC_API_KEY="sk-ant-old"\n'
            '# after\n'
        )
        key = "sk-ant-new-" + "q" * 20
        ok, _ = mm.ModelManager().save_api_key(key)
        assert ok is True
        contents = zshrc.read_text()
        assert "sk-ant-old" not in contents
        assert key in contents
        # Surrounding comments preserved.
        assert "# before" in contents
        assert "# after" in contents

    def test_atomic_write_uses_tmp_then_rename(self, home):
        """After save, no .tmp file should be left behind."""
        key = "sk-ant-atomic-" + "p" * 16
        ok, _ = mm.ModelManager().save_api_key(key)
        assert ok is True
        assert not (home / ".zshrc.tmp").exists()
