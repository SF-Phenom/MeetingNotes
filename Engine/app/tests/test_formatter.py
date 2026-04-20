"""Tests for formatter pure functions."""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.formatter import format_transcript, slugify


@dataclass
class _StubTranscription:
    plain_text: str = "Hello world"
    timestamped_text: str = "Hello world"
    duration_minutes: int = 5
    srt_path: str = ""


@dataclass
class _StubSummary:
    title: str = "LLM Derived Title"
    summary: str = "Meeting summary"
    action_items: list = field(default_factory=list)
    key_decisions: list = field(default_factory=list)
    projects_mentioned: list = field(default_factory=list)
    fell_back: bool = False
    model_used: str = "claude-sonnet-4"


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


class TestFormatTranscriptTitle:
    """Formatter treats metadata['title'] as authoritative — pipeline
    resolves the calendar > LLM > source priority + Part N suffix before
    calling in, so formatter shouldn't second-guess it."""

    def test_metadata_title_wins_over_summary(self):
        out = format_transcript(
            transcription=_StubTranscription(),
            summary=_StubSummary(title="LLM Title"),
            metadata={"title": "Pipeline Resolved Title"},
            source="zoom",
            date_str="2026-04-20",
            time_str="14-30",
        )
        assert "# Pipeline Resolved Title" in out
        assert "# LLM Title" not in out

    def test_summary_used_when_no_metadata_title(self):
        out = format_transcript(
            transcription=_StubTranscription(),
            summary=_StubSummary(title="LLM Title"),
            metadata={},
            source="zoom",
            date_str="2026-04-20",
            time_str="14-30",
        )
        assert "# LLM Title" in out

    def test_source_fallback_when_no_title_sources(self):
        out = format_transcript(
            transcription=_StubTranscription(),
            summary=None,
            metadata={},
            source="zoom",
            date_str="2026-04-20",
            time_str="14-30",
        )
        assert "# Zoom Meeting" in out


class TestFormatTranscriptCalendarEventId:
    """calendar_event_id needs to land in the frontmatter when present
    (so _find_prior_parts can detect Part N collisions) and must be absent
    otherwise (so transcripts without calendar association stay clean)."""

    def test_event_id_in_frontmatter_when_present(self):
        out = format_transcript(
            transcription=_StubTranscription(),
            summary=_StubSummary(),
            metadata={"title": "Weekly Sync", "calendar_event_id": "abc-123"},
            source="zoom",
            date_str="2026-04-20",
            time_str="14-30",
        )
        assert "calendar_event_id: abc-123" in out

    def test_no_event_id_line_when_absent(self):
        out = format_transcript(
            transcription=_StubTranscription(),
            summary=_StubSummary(),
            metadata={"title": "Ad-hoc"},
            source="manual",
            date_str="2026-04-20",
            time_str="14-30",
        )
        assert "calendar_event_id:" not in out
