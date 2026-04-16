"""Tests for formatter pure functions."""
from __future__ import annotations

import pytest

from app.formatter import slugify


class TestSlugify:
    def test_simple_title(self):
        assert slugify("Weekly PMM Sync") == "weekly-pmm-sync"

    def test_lowercases(self):
        assert slugify("ALL CAPS") == "all-caps"

    def test_strips_punctuation(self):
        assert slugify("Q1 Review: Results & Next Steps!") == "q1-review-results-next-steps"

    def test_collapses_hyphens(self):
        assert slugify("a  --  b") == "a-b"

    def test_strips_leading_trailing_hyphens(self):
        assert slugify("---hello---") == "hello"

    def test_underscores_become_hyphens(self):
        assert slugify("foo_bar_baz") == "foo-bar-baz"

    def test_empty_returns_untitled(self):
        assert slugify("") == "untitled"

    def test_only_punctuation_returns_untitled(self):
        assert slugify("!!!???") == "untitled"

    def test_whitespace_only_returns_untitled(self):
        assert slugify("   ") == "untitled"

    def test_unicode_word_chars_preserved(self):
        # \w matches unicode letters; non-ASCII should survive as-is (lowercased).
        assert slugify("Café meeting") == "café-meeting"

    @pytest.mark.parametrize(
        "inp,expected",
        [
            ("one", "one"),
            ("one two", "one-two"),
            ("one  two  three", "one-two-three"),
        ],
    )
    def test_spacing_variations(self, inp, expected):
        assert slugify(inp) == expected
