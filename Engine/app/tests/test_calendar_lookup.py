"""Tests for calendar_lookup._select_event and _parse_event_time.

The network-bound paths (_get_credentials, lookup_meeting) are intentionally
not covered here — those depend on Google API mocks that would exercise the
library more than our logic. The association rule + tiebreaker is the
load-bearing decision, and that's what these tests lock down.
"""
from __future__ import annotations

from datetime import datetime

from app.calendar_lookup import _parse_event_time, _select_event


def _event(
    start_iso: str | None,
    end_iso: str | None,
    summary: str = "Test event",
    event_id: str = "evt-1",
    all_day: bool = False,
) -> dict:
    """Build an event dict shaped like the Google Calendar API response."""
    start: dict = {}
    end: dict = {}
    if all_day:
        # Google uses 'date' (no time) for all-day; we should skip these.
        if start_iso:
            start["date"] = start_iso
        if end_iso:
            end["date"] = end_iso
    else:
        if start_iso:
            start["dateTime"] = start_iso
        if end_iso:
            end["dateTime"] = end_iso
    return {"id": event_id, "summary": summary, "start": start, "end": end}


class TestParseEventTime:
    def test_parses_datetime_with_offset(self):
        event = _event("2026-04-20T14:30:00-04:00", "2026-04-20T15:00:00-04:00")
        # tzinfo is stripped (naive local) — downstream comparisons are naive.
        result = _parse_event_time(event, "start")
        assert result == datetime(2026, 4, 20, 14, 30)

    def test_all_day_event_returns_none(self):
        event = _event("2026-04-20", "2026-04-21", all_day=True)
        assert _parse_event_time(event, "start") is None
        assert _parse_event_time(event, "end") is None

    def test_missing_datetime_returns_none(self):
        assert _parse_event_time({"start": {}}, "start") is None
        assert _parse_event_time({}, "start") is None

    def test_malformed_datetime_returns_none(self):
        event = _event("not-a-timestamp", "also-bad")
        assert _parse_event_time(event, "start") is None


class TestSelectEvent:
    """The association rule: event.start - 10min <= recording_start <= event.end."""

    def test_matches_event_when_recording_in_window(self):
        events = [_event("2026-04-20T14:30:00-04:00", "2026-04-20T15:00:00-04:00")]
        # Recording at 2:25 PM — within the pre-buffer (2:20 PM) and before end.
        chosen = _select_event(events, datetime(2026, 4, 20, 14, 25))
        assert chosen is not None
        assert chosen["id"] == "evt-1"

    def test_no_match_when_recording_too_early(self):
        events = [_event("2026-04-20T14:30:00-04:00", "2026-04-20T15:00:00-04:00")]
        # Recording at 2:15 PM — earlier than event.start - 10 min (2:20).
        assert _select_event(events, datetime(2026, 4, 20, 14, 15)) is None

    def test_no_match_when_recording_after_end(self):
        events = [_event("2026-04-20T14:30:00-04:00", "2026-04-20T15:00:00-04:00")]
        # Recording at 3:05 PM — after event.end.
        assert _select_event(events, datetime(2026, 4, 20, 15, 5)) is None

    def test_recording_exactly_at_event_end_matches(self):
        """Inclusive boundary on the end side — the rule is <=, not <."""
        events = [_event("2026-04-20T14:30:00-04:00", "2026-04-20T15:00:00-04:00")]
        assert _select_event(events, datetime(2026, 4, 20, 15, 0)) is not None

    def test_recording_exactly_at_pre_buffer_edge_matches(self):
        """Inclusive boundary on the pre-buffer side (10 min before start)."""
        events = [_event("2026-04-20T14:30:00-04:00", "2026-04-20T15:00:00-04:00")]
        assert _select_event(events, datetime(2026, 4, 20, 14, 20)) is not None

    def test_late_starting_meeting_still_matches(self):
        """A 1-1 scheduled 2:30-3:00 that starts late; user records at 2:55.
        Recording is in [2:20, 3:00] — still associated per the rule."""
        events = [_event("2026-04-20T14:30:00-04:00", "2026-04-20T15:00:00-04:00")]
        chosen = _select_event(events, datetime(2026, 4, 20, 14, 55))
        assert chosen is not None
        assert chosen["id"] == "evt-1"

    def test_back_to_back_prefers_upcoming_meeting(self):
        """Meeting A 1:00-1:30, Meeting B 1:30-2:30. Recording at 1:26 — both
        qualify per the rule (B because recording >= 1:20 = B.start - 10min).
        Tiebreaker picks B (the upcoming one, not the tail of A)."""
        events = [
            _event("2026-04-20T13:00:00-04:00", "2026-04-20T13:30:00-04:00",
                   event_id="meeting-a"),
            _event("2026-04-20T13:30:00-04:00", "2026-04-20T14:30:00-04:00",
                   event_id="meeting-b"),
        ]
        chosen = _select_event(events, datetime(2026, 4, 20, 13, 26))
        assert chosen is not None
        assert chosen["id"] == "meeting-b"

    def test_all_day_events_ignored(self):
        """All-day "PTO" / "Focus Time" blocks shouldn't title a recording
        just because the recording falls inside their window."""
        events = [_event("2026-04-20", "2026-04-21", all_day=True)]
        assert _select_event(events, datetime(2026, 4, 20, 14, 30)) is None

    def test_empty_list_returns_none(self):
        assert _select_event([], datetime(2026, 4, 20, 14, 30)) is None

    def test_single_in_progress_event_wins_without_tiebreaker(self):
        """Only one qualifying event that's already started — the 'upcoming'
        branch is empty, fallback picks the closest-start event."""
        events = [
            _event("2026-04-20T14:00:00-04:00", "2026-04-20T15:00:00-04:00",
                   event_id="running"),
        ]
        chosen = _select_event(events, datetime(2026, 4, 20, 14, 40))
        assert chosen is not None
        assert chosen["id"] == "running"

    def test_prefers_earliest_upcoming_when_multiple_upcoming(self):
        """Three upcoming events — tiebreaker takes the earliest start."""
        events = [
            _event("2026-04-20T14:30:00-04:00", "2026-04-20T15:00:00-04:00",
                   event_id="earlier"),
            _event("2026-04-20T14:35:00-04:00", "2026-04-20T15:05:00-04:00",
                   event_id="later"),
        ]
        chosen = _select_event(events, datetime(2026, 4, 20, 14, 25))
        assert chosen is not None
        assert chosen["id"] == "earlier"
