"""Tests for calendar_lookup — association rule, scope checks, reset, probe.

The network-bound "ok" path of test_connection isn't covered — mocking the
full Google API would exercise the library more than our logic. The
decision-bearing code (association rule, scope comparison, early-exit
branches of test_connection) is what these tests lock down.
"""
from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import calendar_lookup
from app.calendar_lookup import (
    _parse_event_time,
    _scopes_satisfy,
    _select_event,
    reset_auth,
)

# Alias to avoid pytest collecting ``test_connection`` as a module-level test.
probe_connection = calendar_lookup.test_connection


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


class TestScopesSatisfy:
    """SCOPES is a list of URLs; google-auth stores granted scopes on creds.scopes.
    We require every scope in SCOPES to be granted. Extras OK (future-proof)."""

    def test_exact_match_satisfies(self):
        creds = SimpleNamespace(
            scopes=["https://www.googleapis.com/auth/calendar.readonly"]
        )
        assert _scopes_satisfy(creds) is True

    def test_superset_satisfies(self):
        """A token granted more than we need still works."""
        creds = SimpleNamespace(scopes=[
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/userinfo.email",
        ])
        assert _scopes_satisfy(creds) is True

    def test_legacy_narrow_scope_rejected(self):
        """Tokens granted under the old calendar.events.readonly scope
        are the whole reason this check exists — must be rejected."""
        creds = SimpleNamespace(scopes=[
            "https://www.googleapis.com/auth/calendar.events.readonly"
        ])
        assert _scopes_satisfy(creds) is False

    def test_none_scopes_rejected(self):
        """google-auth sometimes sets creds.scopes=None (e.g. malformed token)."""
        creds = SimpleNamespace(scopes=None)
        assert _scopes_satisfy(creds) is False

    def test_missing_attribute_rejected(self):
        """Defensive: don't blow up on a credentials-shaped object that
        lacks .scopes entirely."""
        creds = SimpleNamespace()
        assert _scopes_satisfy(creds) is False


class TestResetAuth:
    @pytest.fixture
    def token_at(self, tmp_path, monkeypatch):
        """Redirect TOKEN_PATH at a throwaway file inside tmp_path."""
        from app import calendar_lookup
        path = tmp_path / "google_token.json"
        monkeypatch.setattr(calendar_lookup, "TOKEN_PATH", str(path))
        return path

    def test_removes_existing_token(self, token_at):
        token_at.write_text('{"token": "abc"}')
        assert reset_auth() is True
        assert not token_at.exists()

    def test_noop_when_no_token(self, token_at):
        assert not token_at.exists()
        assert reset_auth() is False


class TestTestConnection:
    """test_connection has three decision branches we can cover without
    mocking the full Google API: no token, scope outdated, and import missing.
    The 'ok' path requires hitting real Google endpoints — out of scope here."""

    @pytest.fixture
    def token_at(self, tmp_path, monkeypatch):
        from app import calendar_lookup
        path = tmp_path / "google_token.json"
        monkeypatch.setattr(calendar_lookup, "TOKEN_PATH", str(path))
        return path

    def test_no_token_reports_not_authorized(self, token_at):
        assert not token_at.exists()
        probe = probe_connection()
        assert probe["status"] == "not_authorized"
        assert "Sign in" in probe["detail"]

    def test_legacy_scope_token_reports_scope_outdated(self, token_at, monkeypatch):
        """A saved token with only calendar.events.readonly should trigger
        the scope_outdated path so the UI can offer re-auth."""
        # Build a minimal token file that google-auth can parse. The refresh
        # token / client id are dummies; we never execute an API call — the
        # probe bails out when _get_credentials returns None due to scope.
        token_at.write_text(json.dumps({
            "token": "stale",
            "refresh_token": "refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "dummy",
            "client_secret": "dummy",
            "scopes": ["https://www.googleapis.com/auth/calendar.events.readonly"],
        }))

        # Force _get_credentials to return None so the probe enters the
        # scope-inspection branch. (Without this, google-auth might try to
        # refresh the token and hit the network.)
        from app import calendar_lookup
        monkeypatch.setattr(calendar_lookup, "_get_credentials", lambda **kw: None)

        probe = probe_connection()
        assert probe["status"] == "scope_outdated"
        assert "calendar.readonly" in probe["detail"]
