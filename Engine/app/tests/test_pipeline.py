"""Tests for pipeline pure functions (filename parsing, etc).

Integration tests for `process_recording` live in test_pipeline_benchmark.py
and require the benchmark WAV fixture.
"""
from __future__ import annotations

from datetime import datetime

from app.pipeline import _parse_filename


class TestParseFilename:
    def test_canonical_format(self):
        source, date, time = _parse_filename("zoom_2026-03-31_10-02.wav")
        assert source == "zoom"
        assert date == "2026-03-31"
        assert time == "10-02"

    def test_ignores_directory_path(self):
        source, date, time = _parse_filename(
            "/Users/me/Engine/recordings/queue/teams_2026-01-15_14-30.wav"
        )
        assert source == "teams"
        assert date == "2026-01-15"
        assert time == "14-30"

    def test_ignores_extension(self):
        # Works for any extension since we strip it.
        source, date, time = _parse_filename("meet_2026-12-01_09-00.mp3")
        assert source == "meet"
        assert date == "2026-12-01"
        assert time == "09-00"

    def test_source_with_underscore(self):
        # The regex allows [a-zA-Z0-9_]+ in source.
        source, date, time = _parse_filename("google_meet_2026-03-31_10-02.wav")
        assert source == "google_meet"
        assert date == "2026-03-31"
        assert time == "10-02"

    def test_unparseable_returns_defaults(self):
        source, date, time = _parse_filename("something_weird.wav")
        assert source == "unknown"
        # Defaults are today's date / now's time — validate shape only.
        assert len(date) == 10 and date[4] == "-" and date[7] == "-"
        assert len(time) == 5 and time[2] == "-"
        # Parseable as date.
        datetime.strptime(date, "%Y-%m-%d")
        datetime.strptime(time, "%H-%M")

    def test_empty_returns_defaults(self):
        source, _, _ = _parse_filename("")
        assert source == "unknown"

    def test_missing_time_falls_back(self):
        # No time portion — should not match, defaults returned.
        source, _, _ = _parse_filename("zoom_2026-03-31.wav")
        assert source == "unknown"
