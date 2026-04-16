"""Tests for summarizer pure functions."""
from __future__ import annotations

import pytest

from app.summarizer import _extract_json


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
