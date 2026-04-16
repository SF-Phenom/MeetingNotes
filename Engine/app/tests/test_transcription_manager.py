"""Tests for TranscriptionManager — flag lifecycle + hang-proof cleanup.

pipeline.process_recording and RealtimeTranscriber are heavy dependencies
(MLX + osascript), so we monkeypatch them. The manager's value is the
flag invariants and the dispatch plumbing; that's what we assert.
"""
from __future__ import annotations

import threading
import time

import pytest

from app import transcription_manager as tm_mod
from app.transcription_manager import TranscriptionManager
from app.ui_bridge import UIBridge


class _FakeRealtime:
    """Mock RealtimeTranscriber — tracks start/stop + the returned text."""

    def __init__(self) -> None:
        self.started_path: str | None = None
        self.stopped = False
        self.live_transcript_path: str | None = None
        self._stop_return = "realtime text"
        self.raise_on_start = False
        self.raise_on_stop = False

    def start(self, wav_path: str) -> None:
        if self.raise_on_start:
            raise RuntimeError("boom-start")
        self.started_path = wav_path
        self.live_transcript_path = wav_path + ".live.txt"

    def stop(self) -> str:
        self.stopped = True
        if self.raise_on_stop:
            raise RuntimeError("boom-stop")
        return self._stop_return


@pytest.fixture
def fake_realtime_cls(monkeypatch):
    """Replace RealtimeTranscriber with a factory we can inspect."""
    instances: list[_FakeRealtime] = []

    def _factory():
        fake = _FakeRealtime()
        instances.append(fake)
        return fake

    monkeypatch.setattr(tm_mod, "RealtimeTranscriber", _factory)
    return instances


@pytest.fixture
def notify_log():
    """Capture (subtitle, message) pairs the manager emits."""
    calls: list[tuple[str, str]] = []
    def _notify(subtitle: str, message: str) -> None:
        calls.append((subtitle, message))
    return calls, _notify


@pytest.fixture
def rebuild_log():
    """Capture every menu-rebuild request."""
    calls: list[None] = []
    def _rebuild() -> None:
        calls.append(None)
    return calls, _rebuild


# ---------------------------------------------------------------------------
# Realtime lifecycle
# ---------------------------------------------------------------------------


class TestRealtime:
    def test_start_success(self, fake_realtime_cls, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        assert m.start_realtime("/tmp/foo.wav") is True
        assert fake_realtime_cls[0].started_path == "/tmp/foo.wav"
        assert m.realtime_live_transcript_path == "/tmp/foo.wav.live.txt"

    def test_start_failure_returns_false(self, fake_realtime_cls, notify_log, rebuild_log, monkeypatch):
        _, notify = notify_log
        _, rebuild = rebuild_log

        def _explode():
            raise RuntimeError("nope")

        monkeypatch.setattr(tm_mod, "RealtimeTranscriber", _explode)
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        assert m.start_realtime("/tmp/foo.wav") is False
        assert m.realtime_live_transcript_path is None

    def test_stop_returns_accumulated_text(self, fake_realtime_cls, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.start_realtime("/tmp/x.wav")
        assert m.stop_realtime() == "realtime text"
        assert fake_realtime_cls[0].stopped is True

    def test_stop_without_start_returns_none(self, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        assert m.stop_realtime() is None

    def test_stop_swallows_exception(self, fake_realtime_cls, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.start_realtime("/tmp/x.wav")
        fake_realtime_cls[0].raise_on_stop = True
        assert m.stop_realtime() is None
        # Manager resets _realtime to None afterward so next start is clean.
        assert m.realtime_live_transcript_path is None

    def test_shutdown_stops_realtime(self, fake_realtime_cls, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.start_realtime("/tmp/x.wav")
        m.shutdown()
        assert fake_realtime_cls[0].stopped is True
        assert m.realtime_live_transcript_path is None


# ---------------------------------------------------------------------------
# Batch: is_transcribing invariants
# ---------------------------------------------------------------------------


def _wait_until(predicate, timeout=3.0):
    """Block up to ``timeout`` seconds until ``predicate()`` is truthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class TestBatchFlag:
    def test_starts_false(self, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        assert m.is_transcribing is False

    def test_flag_cleared_on_successful_batch(self, monkeypatch, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log

        def _pipeline(wav_path, pre_transcribed_text=None):
            return "/tmp/out.md"

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        m = TranscriptionManager(UIBridge(), rebuild, notify)
        assert m.submit(["/tmp/a.wav"]) is True
        assert _wait_until(lambda: not m.is_transcribing)

    def test_flag_cleared_even_when_pipeline_raises(self, monkeypatch, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log

        def _pipeline(wav_path, pre_transcribed_text=None):
            raise RuntimeError("simulate pipeline crash")

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.submit(["/tmp/a.wav"])
        assert _wait_until(lambda: not m.is_transcribing), (
            "flag must reset even when pipeline raises — this is the "
            "hang-proof guarantee TranscriptionManager provides"
        )

    def test_submit_while_running_is_rejected(self, monkeypatch, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        block = threading.Event()
        release = threading.Event()

        def _pipeline(wav_path, pre_transcribed_text=None):
            block.set()
            release.wait(timeout=5)
            return "/tmp/out.md"

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.submit(["/tmp/slow.wav"])
        block.wait(timeout=2)  # wait until pipeline is running
        assert m.submit(["/tmp/other.wav"]) is False, "concurrent submit should be rejected"
        release.set()
        _wait_until(lambda: not m.is_transcribing)


# ---------------------------------------------------------------------------
# Batch: notifications + rebuild_menu
# ---------------------------------------------------------------------------


class TestBatchSignals:
    def test_success_notification(self, monkeypatch, notify_log, rebuild_log):
        notifications, notify = notify_log
        rebuilds, rebuild = rebuild_log
        monkeypatch.setattr(
            tm_mod.pipeline, "process_recording",
            lambda p, pre_transcribed_text=None: "/tmp/out.md",
        )
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav"])
        _wait_until(lambda: not m.is_transcribing)
        # Drain the bridge's queued callables so dispatch-side effects run.
        bridge.drain()

        # Expect at least one rebuild (pre-start) + one completion rebuild.
        assert len(rebuilds) >= 2
        # Expect exactly one success notification.
        assert notifications == [("1 transcript ready", "out.md")]

    def test_failure_notification(self, monkeypatch, notify_log, rebuild_log):
        notifications, notify = notify_log
        _, rebuild = rebuild_log
        monkeypatch.setattr(
            tm_mod.pipeline, "process_recording",
            lambda p, pre_transcribed_text=None: None,  # pipeline returned None
        )
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        assert ("Transcription errors", "1 recording failed — check logs") in notifications

    def test_checkin_notification(self, monkeypatch, notify_log, rebuild_log):
        notifications, notify = notify_log
        _, rebuild = rebuild_log
        monkeypatch.setattr(
            tm_mod.pipeline, "process_recording",
            lambda p, pre_transcribed_text=None: "/tmp/out.md",
        )
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: True)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        assert any(sub == "Check-in ready" for sub, _ in notifications)

    def test_pre_transcribed_text_only_used_on_first(self, monkeypatch, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        seen: list[tuple[str, str | None]] = []

        def _pipeline(wav_path, pre_transcribed_text=None):
            seen.append((wav_path, pre_transcribed_text))
            return "/tmp/out.md"

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.submit(["/tmp/a.wav", "/tmp/b.wav", "/tmp/c.wav"], pre_transcribed_text="HELLO")
        _wait_until(lambda: not m.is_transcribing)

        assert seen == [
            ("/tmp/a.wav", "HELLO"),
            ("/tmp/b.wav", None),
            ("/tmp/c.wav", None),
        ]
