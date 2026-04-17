"""Tests for summarizer pure functions."""
from __future__ import annotations

import pytest

import anthropic

from app import summarizer
from app.summarizer import _extract_json, validate_api_key


class TestExtractJson:
    def test_bare_json(self):
        result = _extract_json('{"foo": "bar", "n": 42}')
        assert result == {"foo": "bar", "n": 42}

    def test_fenced_json_block(self):
        text = '```json\n{"title": "Weekly sync", "items": []}\n```'
        assert _extract_json(text) == {"title": "Weekly sync", "items": []}

    def test_unlabeled_fence(self):
        text = '```\n{"title": "t"}\n```'
        assert _extract_json(text) == {"title": "t"}

    def test_strips_qwen_thinking_tags(self):
        text = (
            "<think>Let me parse this transcript...</think>\n"
            '{"summary": "A short summary."}'
        )
        assert _extract_json(text) == {"summary": "A short summary."}

    def test_multiline_thinking_tag(self):
        text = (
            "<think>\n"
            "Line 1\n"
            "Line 2\n"
            "</think>\n"
            '{"x": 1}'
        )
        assert _extract_json(text) == {"x": 1}

    def test_extracts_from_surrounding_prose(self):
        text = (
            "Sure, here's the summary:\n\n"
            '{"title": "x", "action_items": [{"item": "ship it"}]}\n\n'
            "Let me know if you need anything else."
        )
        assert _extract_json(text) == {
            "title": "x",
            "action_items": [{"item": "ship it"}],
        }

    def test_nested_json(self):
        text = '{"outer": {"inner": {"deep": [1, 2, 3]}}}'
        assert _extract_json(text) == {"outer": {"inner": {"deep": [1, 2, 3]}}}

    def test_invalid_json_raises(self):
        with pytest.raises(Exception):
            _extract_json("this is not json at all")

    def test_malformed_fence_content_raises(self):
        with pytest.raises(Exception):
            _extract_json("```json\n{not valid json}\n```")

    def test_fenced_block_preferred_over_stray_braces(self):
        # If there's a fenced block, we use it even if there are other {...}
        # in the surrounding prose.
        text = (
            "Raw notes: {partial}\n"
            '```json\n{"real": "content"}\n```\n'
            "Trailing {junk}."
        )
        assert _extract_json(text) == {"real": "content"}


class TestValidateApiKey:
    """Round-trip validation against the Anthropic API.

    The actual SDK is real but we patch ``Anthropic.models.list`` so each
    test deterministically raises (or succeeds) without a network call.
    """

    @pytest.fixture
    def patch_models_list(self, monkeypatch):
        """Replace anthropic.Anthropic so models.list does what we want."""

        def _factory(side_effect):
            class _StubModels:
                def list(self_inner):
                    if isinstance(side_effect, BaseException):
                        raise side_effect
                    return side_effect

            class _StubClient:
                def __init__(self_inner, *args, **kwargs):
                    self_inner.models = _StubModels()

            monkeypatch.setattr(anthropic, "Anthropic", _StubClient)

        return _factory

    def test_success(self, patch_models_list):
        patch_models_list(["model-a"])  # any non-exception value
        ok, msg = validate_api_key("sk-ant-good")
        assert ok is True
        assert "validated" in msg.lower()

    def test_authentication_error_rejected(self, patch_models_list):
        # AuthenticationError signature requires message + response + body in
        # current SDK; constructing one directly is fiddly, so use a generic
        # APIError subclass with the right name via a minimal raise path.
        err = anthropic.AuthenticationError.__new__(anthropic.AuthenticationError)
        BaseException.__init__(err, "401 invalid x-api-key")
        patch_models_list(err)
        ok, msg = validate_api_key("sk-ant-bad")
        assert ok is False
        assert "rejected" in msg.lower()

    def test_network_error_treated_as_soft_ok(self, patch_models_list):
        err = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
        BaseException.__init__(err, "connection refused")
        patch_models_list(err)
        ok, msg = validate_api_key("sk-ant-offline")
        assert ok is True
        assert "not validated" in msg.lower()


class TestSummarizeFallback:
    """Automatic mode must mark the SummaryResult so the UI can surface
    the Claude→Ollama degradation."""

    def _ollama_result(self) -> summarizer.SummaryResult:
        return summarizer.SummaryResult(
            title="t", summary="s", action_items=[],
            projects_mentioned=[], key_decisions=[],
            model_used="ollama:qwen3:4b",
        )

    def test_fell_back_set_on_claude_failure(self, monkeypatch):
        monkeypatch.setattr(summarizer, "_model_preference", "automatic")
        monkeypatch.setattr(
            summarizer, "_summarize_claude",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("claude down")),
        )
        monkeypatch.setattr(
            summarizer, "_summarize_ollama",
            lambda *a, **k: self._ollama_result(),
        )
        result = summarizer.summarize("text", "context", {})
        assert result.fell_back is True
        assert result.model_used.startswith("ollama:")

    def test_fell_back_false_when_claude_succeeds(self, monkeypatch):
        monkeypatch.setattr(summarizer, "_model_preference", "automatic")
        claude_result = summarizer.SummaryResult(
            title="t", summary="s", action_items=[],
            projects_mentioned=[], key_decisions=[],
            model_used="claude:sonnet",
        )
        monkeypatch.setattr(
            summarizer, "_summarize_claude", lambda *a, **k: claude_result,
        )
        result = summarizer.summarize("text", "context", {})
        assert result.fell_back is False
