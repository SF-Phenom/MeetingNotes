"""Tests for the pipeline-layer routing that picks which diarizer model runs.

Two pieces under test:
  * ``pipeline._build_engine_hints`` — turns the enriched metadata dict
    (which carries ``participants`` as a ", "-joined display-name string)
    into a small hints dict the batch engine consumes.
  * ``transcriber._pick_diarizer_model`` — picks sortformer for small
    meetings (≤4 total attendees) and community-1 otherwise.
"""
from __future__ import annotations

import pytest

from app.pipeline import _build_engine_hints
from app.transcriber import _pick_diarizer_model


class TestBuildEngineHints:
    def test_no_participants_returns_empty(self):
        assert _build_engine_hints({}) == {}

    def test_empty_participants_string_returns_empty(self):
        assert _build_engine_hints({"participants": ""}) == {}
        assert _build_engine_hints({"participants": "   "}) == {}

    def test_non_string_participants_returns_empty(self):
        # Defensive: if metadata ever carries a list or None, we shouldn't crash.
        assert _build_engine_hints({"participants": None}) == {}
        assert _build_engine_hints({"participants": ["Alice", "Bob"]}) == {}

    def test_single_other_attendee(self):
        # "Alice" = 1 other + user = 2 total.
        assert _build_engine_hints({"participants": "Alice"}) == {"participant_count": 2}

    def test_multiple_others(self):
        # 3 others + user = 4 total.
        hints = _build_engine_hints({"participants": "Alice, Bob, Carol"})
        assert hints == {"participant_count": 4}

    def test_trailing_whitespace_and_empty_slots_ignored(self):
        # Calendar enrichment should produce clean ", " joins, but defensively
        # handle stray whitespace and empty entries.
        hints = _build_engine_hints(
            {"participants": " Alice ,  , Bob ,"}
        )
        # Two real names + user = 3.
        assert hints == {"participant_count": 3}


class TestPickDiarizerModel:
    def test_no_hints_is_community_1(self):
        assert _pick_diarizer_model(None) == "community-1"
        assert _pick_diarizer_model({}) == "community-1"

    def test_missing_count_is_community_1(self):
        assert _pick_diarizer_model({"some_other_key": 3}) == "community-1"

    def test_small_meeting_is_sortformer(self):
        # Threshold is ≤4 total attendees.
        for count in (1, 2, 3, 4):
            assert _pick_diarizer_model({"participant_count": count}) == "sortformer"

    def test_large_meeting_is_community_1(self):
        for count in (5, 6, 20, 100):
            assert _pick_diarizer_model({"participant_count": count}) == "community-1"

    def test_zero_or_negative_is_community_1(self):
        # Defensive: a bad count never picks the 4-speaker-capped model.
        assert _pick_diarizer_model({"participant_count": 0}) == "community-1"
        assert _pick_diarizer_model({"participant_count": -3}) == "community-1"

    def test_non_int_count_is_community_1(self):
        # A string or float sneaking through should not be honored.
        assert _pick_diarizer_model({"participant_count": "3"}) == "community-1"
        assert _pick_diarizer_model({"participant_count": 3.0}) == "community-1"
