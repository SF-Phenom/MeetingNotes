"""Tests for RealtimeTranscriber — non-GPU-path behavior only."""
from __future__ import annotations

import os

from app.realtime_transcriber import RealtimeTranscriber


class TestWriteLiveTranscriptRaceGuard:
    """`_write_live_transcript` must no-op after stop() has fired.

    A long-running Parakeet cycle can finish after stop() deleted the
    `.live.txt` sidecar; if the post-cycle write ran anyway, the file
    would reappear and orphan in `recordings/active/`.
    """

    def test_writes_when_stop_event_clear(self, tmp_path):
        rt = RealtimeTranscriber()
        rt._live_txt_path = str(tmp_path / "foo.live.txt")
        rt._write_live_transcript("hello")
        assert os.path.exists(rt._live_txt_path)
        with open(rt._live_txt_path) as f:
            assert f.read() == "hello\n"

    def test_noop_when_stop_event_set(self, tmp_path):
        rt = RealtimeTranscriber()
        rt._live_txt_path = str(tmp_path / "foo.live.txt")
        rt._stop_event.set()
        rt._write_live_transcript("hello")
        assert not os.path.exists(rt._live_txt_path)

    def test_noop_when_path_is_none(self):
        # Pre-start() calls should also not explode.
        rt = RealtimeTranscriber()
        assert rt._live_txt_path is None
        rt._write_live_transcript("hello")  # no-op, no crash
