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
from app.transcription_manager import RealtimeResult, TranscriptionManager
from app.transcript_formatter import Sentence
from app.ui_bridge import UIBridge


class _FakeRealtime:
    """Mock RealtimeTranscriber — tracks start/stop + the returned text."""

    def __init__(self) -> None:
        self.started_path: str | None = None
        self.stopped = False
        self.live_transcript_path: str | None = None
        self._stop_return = "realtime text"
        self._sentences: list[Sentence] = []
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

    @property
    def accumulated_sentences(self) -> list[Sentence]:
        return self._sentences


@pytest.fixture
def fake_realtime_cls(monkeypatch):
    """Replace the realtime-engine factory with a fake we can inspect.

    TranscriptionManager calls get_realtime_engine() (imported from
    transcription_engine) to build the realtime transcriber. We patch that
    inside tm_mod so the manager sees our fake, not a real Parakeet.
    """
    instances: list[_FakeRealtime] = []

    def _factory():
        fake = _FakeRealtime()
        instances.append(fake)
        return fake

    monkeypatch.setattr(tm_mod, "get_realtime_engine", _factory)
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

        monkeypatch.setattr(tm_mod, "get_realtime_engine", _explode)
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        assert m.start_realtime("/tmp/foo.wav") is False
        assert m.realtime_live_transcript_path is None

    def test_stop_returns_accumulated_text(self, fake_realtime_cls, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.start_realtime("/tmp/x.wav")
        result = m.stop_realtime()
        assert result.text == "realtime text"
        assert result.sentences == []
        assert fake_realtime_cls[0].stopped is True

    def test_stop_returns_accumulated_sentences(self, fake_realtime_cls, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.start_realtime("/tmp/x.wav")
        # Prime the fake with sentences the engine would have produced.
        fake_realtime_cls[0]._sentences = [
            Sentence(start=0.0, end=1.0, text="hi"),
            Sentence(start=1.5, end=2.0, text="hello"),
        ]
        result = m.stop_realtime()
        assert len(result.sentences) == 2
        assert [s.text for s in result.sentences] == ["hi", "hello"]

    def test_stop_without_start_returns_empty_result(self, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        result = m.stop_realtime()
        assert result == RealtimeResult(text=None, sentences=[])

    def test_stop_swallows_exception(self, fake_realtime_cls, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.start_realtime("/tmp/x.wav")
        fake_realtime_cls[0].raise_on_stop = True
        result = m.stop_realtime()
        assert result.text is None
        assert result.sentences == []
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

        def _pipeline(wav_path, pre_transcribed_text=None, **_kwargs):
            return "/tmp/out.md"

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        m = TranscriptionManager(UIBridge(), rebuild, notify)
        assert m.submit(["/tmp/a.wav"]) is True
        assert _wait_until(lambda: not m.is_transcribing)

    def test_flag_cleared_even_when_pipeline_raises(self, monkeypatch, notify_log, rebuild_log):
        _, notify = notify_log
        _, rebuild = rebuild_log

        def _pipeline(wav_path, pre_transcribed_text=None, **_kwargs):
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

        def _pipeline(wav_path, pre_transcribed_text=None, **_kwargs):
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
            lambda p, pre_transcribed_text=None, **_kwargs: "/tmp/out.md",
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
            lambda p, pre_transcribed_text=None, **_kwargs: None,
        )
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        assert ("Transcription errors", "1 recording failed — check logs") in notifications

    def test_too_short_notification_instead_of_error(self, monkeypatch, notify_log, rebuild_log):
        """When pipeline fires on_too_short, the manager emits the quiet
        'Recording too short' notification and does NOT count it toward the
        scary 'Transcription errors' bucket."""
        notifications, notify = notify_log
        _, rebuild = rebuild_log

        def _pipeline(wav_path, pre_transcribed_text=None, **kwargs):
            cb = kwargs.get("on_too_short")
            if cb is not None:
                cb()
            return None

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/short.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        subtitles = [sub for sub, _ in notifications]
        assert "Recording too short" in subtitles
        assert "Transcription errors" not in subtitles

    def test_too_short_and_failure_emit_separately(self, monkeypatch, notify_log, rebuild_log):
        """Mixed batch: one too-short + one real failure should each get their
        own notification — not lumped into one bucket."""
        notifications, notify = notify_log
        _, rebuild = rebuild_log

        call_idx = iter(range(2))

        def _pipeline(wav_path, pre_transcribed_text=None, **kwargs):
            n = next(call_idx)
            if n == 0:
                cb = kwargs.get("on_too_short")
                if cb is not None:
                    cb()
            # Both calls return None — first is too-short, second is a "real"
            # failure. The manager should distinguish them via the callback.
            return None

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/short.wav", "/tmp/broken.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        subtitles = [sub for sub, _ in notifications]
        assert "Recording too short" in subtitles
        assert "Transcription errors" in subtitles
        # And neither lumps the count.
        failed_msgs = [m for s, m in notifications if s == "Transcription errors"]
        assert failed_msgs == ["1 recording failed — check logs"]
        too_short_msgs = [m for s, m in notifications if s == "Recording too short"]
        assert too_short_msgs == ["1 recording under 16 sec; nothing to transcribe"]

    def test_too_short_pluralizes_message(self, monkeypatch, notify_log, rebuild_log):
        notifications, notify = notify_log
        _, rebuild = rebuild_log

        def _pipeline(wav_path, pre_transcribed_text=None, **kwargs):
            cb = kwargs.get("on_too_short")
            if cb is not None:
                cb()
            return None

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav", "/tmp/b.wav", "/tmp/c.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        too_short_msgs = [m for s, m in notifications if s == "Recording too short"]
        assert too_short_msgs == ["3 recordings under 16 sec; nothing to transcribe"]

    def test_summary_fallback_notification(self, monkeypatch, notify_log, rebuild_log):
        """When pipeline fires on_summary_fallback, the user gets a separate
        'used Ollama' notification so the model degradation isn't silent."""
        notifications, notify = notify_log
        _, rebuild = rebuild_log

        def _pipeline(wav_path, pre_transcribed_text=None, **kwargs):
            cb = kwargs.get("on_summary_fallback")
            if cb is not None:
                cb()
            return "/tmp/out.md"

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        subtitles = [sub for sub, _ in notifications]
        assert "Summarized with local model" in subtitles

    def test_no_fallback_notification_when_callback_unused(
        self, monkeypatch, notify_log, rebuild_log,
    ):
        notifications, notify = notify_log
        _, rebuild = rebuild_log
        monkeypatch.setattr(
            tm_mod.pipeline, "process_recording",
            lambda p, pre_transcribed_text=None, **_kwargs: "/tmp/out.md",
        )
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        subtitles = [sub for sub, _ in notifications]
        assert "Summarized with local model" not in subtitles

    def test_export_success_notification_aggregates_across_batch(
        self, monkeypatch, notify_log, rebuild_log,
    ):
        """Multiple wavs in one batch should produce a single
        'N items exported' notification summing across all of them."""
        from app.exporter import BACKEND_APPLE_REMINDERS, ExportResult

        notifications, notify = notify_log
        _, rebuild = rebuild_log

        per_wav_counts = iter([2, 5])

        def _pipeline(wav_path, pre_transcribed_text=None, **kwargs):
            cb = kwargs.get("on_export")
            if cb is not None:
                cb(ExportResult(
                    backend=BACKEND_APPLE_REMINDERS,
                    exported_count=next(per_wav_counts),
                ))
            return "/tmp/out.md"

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav", "/tmp/b.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        export_subs = [(sub, msg) for sub, msg in notifications
                       if sub == "Action items exported"]
        assert len(export_subs) == 1
        assert "7 items" in export_subs[0][1]

    def test_export_error_surfaces_first_message(
        self, monkeypatch, notify_log, rebuild_log,
    ):
        from app.exporter import BACKEND_APPLE_REMINDERS, ExportResult

        notifications, notify = notify_log
        _, rebuild = rebuild_log

        def _pipeline(wav_path, pre_transcribed_text=None, **kwargs):
            cb = kwargs.get("on_export")
            if cb is not None:
                cb(ExportResult(
                    backend=BACKEND_APPLE_REMINDERS,
                    exported_count=0,
                    errors=["osascript exited 1: -1728"],
                ))
            return "/tmp/out.md"

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        error_subs = [(sub, msg) for sub, msg in notifications
                      if sub == "Action item export failed"]
        assert len(error_subs) == 1
        assert "1728" in error_subs[0][1]

    def test_no_export_notification_when_callback_unused(
        self, monkeypatch, notify_log, rebuild_log,
    ):
        """When the exporter is disabled, pipeline doesn't fire on_export
        and the user sees no exporter-related notification at all."""
        notifications, notify = notify_log
        _, rebuild = rebuild_log
        monkeypatch.setattr(
            tm_mod.pipeline, "process_recording",
            lambda p, pre_transcribed_text=None, **_kwargs: "/tmp/out.md",
        )
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        bridge = UIBridge()
        m = TranscriptionManager(bridge, rebuild, notify)
        m.submit(["/tmp/a.wav"])
        _wait_until(lambda: not m.is_transcribing)
        bridge.drain()

        subtitles = [sub for sub, _ in notifications]
        assert "Action items exported" not in subtitles
        assert "Action item export failed" not in subtitles

    def test_checkin_notification(self, monkeypatch, notify_log, rebuild_log):
        notifications, notify = notify_log
        _, rebuild = rebuild_log
        monkeypatch.setattr(
            tm_mod.pipeline, "process_recording",
            lambda p, pre_transcribed_text=None, **_kwargs: "/tmp/out.md",
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

        def _pipeline(wav_path, pre_transcribed_text=None, **_kwargs):
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

    def test_pre_transcribed_sentences_only_used_on_first(
        self, monkeypatch, notify_log, rebuild_log,
    ):
        _, notify = notify_log
        _, rebuild = rebuild_log
        seen: list[tuple[str, list | None]] = []

        def _pipeline(wav_path, pre_transcribed_text=None,
                      pre_transcribed_sentences=None, **_kwargs):
            seen.append((wav_path, pre_transcribed_sentences))
            return "/tmp/out.md"

        monkeypatch.setattr(tm_mod.pipeline, "process_recording", _pipeline)
        monkeypatch.setattr(tm_mod.checkin, "should_trigger_checkin", lambda: False)

        sents = [Sentence(start=0.0, end=1.0, text="hi")]
        m = TranscriptionManager(UIBridge(), rebuild, notify)
        m.submit(
            ["/tmp/a.wav", "/tmp/b.wav"],
            pre_transcribed_text="HI",
            pre_transcribed_sentences=sents,
        )
        _wait_until(lambda: not m.is_transcribing)

        # Only first recording gets the sentences — subsequent wavs never
        # saw the realtime engine.
        assert seen[0] == ("/tmp/a.wav", sents)
        assert seen[1] == ("/tmp/b.wav", None)
