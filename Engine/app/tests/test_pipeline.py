"""Tests for pipeline pure functions (filename parsing, etc) plus the
too-short-recording cleanup path, which is small enough to cover without
the benchmark fixture that the full-integration benchmark tests need.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

import pytest

from app.pipeline import _find_prior_parts, _parse_filename, _resolve_base_title


@dataclass
class _StubSummary:
    """Shape-compatible stand-in for summarizer.SummaryResult — only .title
    is read by _resolve_base_title, so the rest of that dataclass is irrelevant."""
    title: str


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


class TestTooShortRecording:
    """Empty pre_transcribed_text means the recording didn't hit Parakeet's
    16-sec realtime chunk threshold. The pipeline should delete the WAV
    (plus any orphan .live.txt sidecar) and fire the on_too_short callback
    so the menubar can show a quiet notification instead of a scary error.
    """

    @pytest.fixture
    def stub_calendar(self, monkeypatch):
        """Calendar enrichment hits the network; neutralise it for unit tests."""
        from app import pipeline as pl
        monkeypatch.setattr(pl, "_calendar_enrich", lambda _path: {})

    def _fake_wav(self, tmp_path):
        wav = tmp_path / "manual_2026-04-17_15-30.wav"
        wav.write_bytes(b"RIFF" + b"\x00" * 100)
        return wav

    def test_deletes_wav_and_returns_none(self, tmp_path, stub_calendar):
        from app import pipeline as pl
        wav = self._fake_wav(tmp_path)
        result = pl.process_recording(str(wav), pre_transcribed_text="")
        assert result is None
        assert not wav.exists()

    def test_fires_on_too_short_callback(self, tmp_path, stub_calendar):
        from app import pipeline as pl
        wav = self._fake_wav(tmp_path)
        fired: list[bool] = []
        pl.process_recording(
            str(wav),
            pre_transcribed_text="",
            on_too_short=lambda: fired.append(True),
        )
        assert fired == [True]

    def test_cleans_up_orphan_live_txt(self, tmp_path, stub_calendar):
        """Belt-and-suspenders: realtime_transcriber.stop() normally removes
        the .live.txt sidecar, but under rare OSError races it can be left
        behind (observed once during 4E back-to-back testing). The too-short
        path is a natural sweep-up point."""
        from app import pipeline as pl
        wav = self._fake_wav(tmp_path)
        live = tmp_path / "manual_2026-04-17_15-30.live.txt"
        live.write_text("partial realtime output")
        pl.process_recording(str(wav), pre_transcribed_text="")
        assert not wav.exists()
        assert not live.exists()

    def test_no_callback_no_crash(self, tmp_path, stub_calendar):
        """on_too_short is optional — omitting it must not break the path."""
        from app import pipeline as pl
        wav = self._fake_wav(tmp_path)
        result = pl.process_recording(str(wav), pre_transcribed_text="")
        assert result is None
        assert not wav.exists()

    def test_callback_error_is_swallowed(self, tmp_path, stub_calendar, caplog):
        """UI hook failures must never surface as pipeline failures —
        the WAV is still gone and the function still returns None."""
        from app import pipeline as pl
        wav = self._fake_wav(tmp_path)
        def _raises() -> None:
            raise RuntimeError("bad UI hook")
        result = pl.process_recording(
            str(wav),
            pre_transcribed_text="",
            on_too_short=_raises,
        )
        assert result is None
        assert not wav.exists()
        assert "on_too_short callback raised" in caplog.text


class TestResolveBaseTitle:
    """Title priority: calendar > LLM > source fallback. The calendar-first
    flip is the whole point of this change — if summary.title wins over a
    real calendar event, we're back to the old behavior."""

    def test_calendar_title_wins_over_summary(self):
        metadata = {"title": "Weekly 1-on-1 with Alex"}
        summary = _StubSummary(title="Catch-up and Project Updates")
        assert _resolve_base_title(metadata, summary, "zoom") == "Weekly 1-on-1 with Alex"

    def test_summary_used_when_no_calendar_title(self):
        metadata = {}
        summary = _StubSummary(title="Ad-hoc Strategy Discussion")
        assert _resolve_base_title(metadata, summary, "zoom") == "Ad-hoc Strategy Discussion"

    def test_source_fallback_when_no_summary_and_no_calendar(self):
        assert _resolve_base_title({}, None, "zoom") == "Zoom Meeting"
        assert _resolve_base_title({}, None, "manual") == "Manual Meeting"

    def test_source_fallback_when_summary_has_empty_title(self):
        summary = _StubSummary(title="")
        assert _resolve_base_title({}, summary, "teams") == "Teams Meeting"

    def test_empty_metadata_title_falls_through(self):
        """A metadata dict with title="" should not claim priority — fall
        through to summary or source just like the key were missing."""
        summary = _StubSummary(title="LLM Derived")
        assert _resolve_base_title({"title": ""}, summary, "zoom") == "LLM Derived"


class TestFindPriorParts:
    """The Part N collision detector. Scans today's transcripts dir for
    matching calendar_event_id in the frontmatter; used by pipeline to
    number the second+ recording of the same meeting.
    """

    def _write_transcript(self, tmp_path, filename: str, event_id: str | None) -> str:
        """Write a minimal transcript with optional calendar_event_id in frontmatter."""
        year_dir = tmp_path / "2026" / "04"
        year_dir.mkdir(parents=True, exist_ok=True)
        path = year_dir / filename
        fm_lines = [
            "---",
            "date: 2026-04-20",
            "time: 2:30 PM",
        ]
        if event_id is not None:
            fm_lines.append(f"calendar_event_id: {event_id}")
        fm_lines.extend(["---", "", "# Some Meeting"])
        path.write_text("\n".join(fm_lines), encoding="utf-8")
        return str(path)

    def test_empty_event_id_returns_empty(self, tmp_path):
        # Short-circuit: no point scanning if there's no event to match.
        self._write_transcript(tmp_path, "2026-04-20_meeting.md", "evt-123")
        assert _find_prior_parts("", "2026-04-20", str(tmp_path)) == []

    def test_no_prior_matches_returns_empty(self, tmp_path):
        self._write_transcript(tmp_path, "2026-04-20_other.md", "different-event")
        assert _find_prior_parts("evt-123", "2026-04-20", str(tmp_path)) == []

    def test_finds_single_prior(self, tmp_path):
        expected = self._write_transcript(
            tmp_path, "2026-04-20_weekly-sync.md", "evt-123"
        )
        result = _find_prior_parts("evt-123", "2026-04-20", str(tmp_path))
        assert result == [expected]

    def test_finds_multiple_priors(self, tmp_path):
        self._write_transcript(tmp_path, "2026-04-20_sync.md", "evt-123")
        self._write_transcript(tmp_path, "2026-04-20_sync-part-2.md", "evt-123")
        assert len(_find_prior_parts("evt-123", "2026-04-20", str(tmp_path))) == 2

    def test_ignores_transcripts_from_other_days(self, tmp_path):
        """A recording from April 19 with the same event_id shouldn't count
        toward a Part N for April 20. Filename prefix is the guard."""
        self._write_transcript(tmp_path, "2026-04-19_sync.md", "evt-123")
        assert _find_prior_parts("evt-123", "2026-04-20", str(tmp_path)) == []

    def test_ignores_non_md_files(self, tmp_path):
        """Stray .tmp files from interrupted atomic writes, or random junk,
        shouldn't be read as transcripts."""
        (tmp_path / "2026" / "04").mkdir(parents=True)
        (tmp_path / "2026" / "04" / "2026-04-20_junk.tmp").write_text(
            "calendar_event_id: evt-123", encoding="utf-8"
        )
        assert _find_prior_parts("evt-123", "2026-04-20", str(tmp_path)) == []

    def test_missing_day_directory_returns_empty(self, tmp_path):
        # transcripts/2026/04/ doesn't exist yet — first recording of the month.
        assert _find_prior_parts("evt-123", "2026-04-20", str(tmp_path)) == []

    def test_malformed_date_returns_empty(self):
        # Don't explode on junk input — the caller shouldn't have to validate.
        assert _find_prior_parts("evt-123", "not-a-date", "/tmp") == []
