"""Tests for pipeline pure functions (filename parsing, etc) plus the
too-short-recording cleanup path, which is small enough to cover without
the benchmark fixture that the full-integration benchmark tests need.
"""
from __future__ import annotations

from datetime import datetime

import pytest

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
