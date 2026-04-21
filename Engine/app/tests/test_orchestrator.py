"""Tests for CallOrchestrator — call-detection + record-prompt state machine."""
from __future__ import annotations

import pytest

from app import orchestrator as orch_mod
from app.orchestrator import CallOrchestrator


class _FakeDetector:
    """Scripted CallDetectorProxy substitute.

    Tests set ``detect_result`` and ``alive`` to control what the detector
    returns. Every call increments a counter so we can assert call shape.
    """
    def __init__(self):
        self.detect_result = None
        self.alive = True
        self.detect_calls = 0
        self.alive_calls: list[str] = []

    def detect_active_call(self):
        self.detect_calls += 1
        return self.detect_result

    def is_call_still_active(self, source: str) -> bool:
        self.alive_calls.append(source)
        return self.alive


class _Recorder:
    def __init__(self):
        self.is_recording_value = False
        self.started: list[tuple[str, str | None]] = []
        self.stopped: list[bool] = []

    def is_recording(self) -> bool:
        return self.is_recording_value

    def start(self, source: str, url: str | None) -> None:
        self.started.append((source, url))
        self.is_recording_value = True

    def stop(self, notify: bool) -> None:
        self.stopped.append(notify)
        self.is_recording_value = False


@pytest.fixture
def setup(monkeypatch, tmp_path):
    """Build a CallOrchestrator with stubbed state_mod + capture hooks."""
    # Isolate the state file to a throwaway location so tests don't touch
    # the real state.json.
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(orch_mod.state_mod, "STATE_PATH", str(state_path))

    detector = _FakeDetector()
    recorder = _Recorder()
    rebuilds: list[None] = []
    notifications: list[tuple[str, str]] = []

    o = CallOrchestrator(
        call_detector=detector,
        is_recording=recorder.is_recording,
        start_recording=recorder.start,
        stop_recording=recorder.stop,
        rebuild_menu=lambda: rebuilds.append(None),
        notify=lambda subtitle, msg: notifications.append((subtitle, msg)),
        debounce_ticks=3,
    )
    return o, detector, recorder, rebuilds, notifications


class TestTickWhenIdle:
    def test_no_call_no_side_effects(self, setup):
        o, detector, _, rebuilds, notifications = setup
        detector.detect_result = None
        o.tick()
        assert o.pending_prompt is None
        assert rebuilds == []
        assert notifications == []

    def test_call_raises_prompt_and_notifies(self, setup):
        o, detector, _, rebuilds, notifications = setup
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()
        assert o.pending_prompt == {"source": "zoom", "url": None}
        assert rebuilds == [None]
        assert notifications == [("zoom call detected", "Click the menubar icon to record.")]

    def test_same_call_repeated_ticks_no_spam(self, setup):
        o, detector, _, rebuilds, notifications = setup
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()
        o.tick()
        o.tick()
        # Only one notification + one rebuild even though detector saw it 3 times.
        assert rebuilds == [None]
        assert notifications == [("zoom call detected", "Click the menubar icon to record.")]

    def test_call_disappears_clears_prompt(self, setup):
        o, detector, _, rebuilds, _ = setup
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()  # prompt raised
        detector.detect_result = None
        o.tick()  # call gone
        assert o.pending_prompt is None
        assert rebuilds == [None, None]  # raise + clear

    def test_suppressed_source_skipped(self, setup):
        from app import state as state_mod
        o, detector, _, rebuilds, notifications = setup
        state_mod.update(suppressed_sources=["zoom"])
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()
        assert o.pending_prompt is None
        assert rebuilds == []
        assert notifications == []


class TestTickWhileRecording:
    def test_still_alive_is_noop(self, setup):
        o, detector, recorder, rebuilds, _ = setup
        # Simulate: user accepted a zoom prompt and is now recording.
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()                 # raise prompt
        o.accept_prompt()        # start recording — fires recorder.start
        recorder.started.clear()
        rebuilds.clear()

        detector.alive = True
        o.tick()
        assert recorder.stopped == []  # did not stop

    def test_debounce_before_stopping(self, setup):
        o, detector, recorder, _, _ = setup
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()
        o.accept_prompt()

        detector.alive = False
        o.tick()  # 1 miss
        assert recorder.stopped == []
        o.tick()  # 2 misses
        assert recorder.stopped == []
        o.tick()  # 3 misses — triggers stop
        assert recorder.stopped == [True]

    def test_alive_resets_debounce(self, setup):
        o, detector, recorder, _, _ = setup
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()
        o.accept_prompt()

        detector.alive = False
        o.tick()
        o.tick()
        # Regain before threshold:
        detector.alive = True
        o.tick()
        # Must not have stopped:
        assert recorder.stopped == []
        # Now re-fall again — need full 3 misses.
        detector.alive = False
        o.tick()
        o.tick()
        assert recorder.stopped == []
        o.tick()
        assert recorder.stopped == [True]


class TestPromptHandlers:
    def test_accept_clears_prompt_and_starts(self, setup):
        o, detector, recorder, _, _ = setup
        detector.detect_result = {"source": "zoom", "url": "https://meet.example"}
        o.tick()
        o.accept_prompt()
        assert o.pending_prompt is None
        assert recorder.started == [("zoom", "https://meet.example")]

    def test_accept_with_no_prompt_is_noop(self, setup):
        o, _, recorder, _, _ = setup
        o.accept_prompt()
        assert recorder.started == []

    def test_skip_clears_prompt_and_remembers_source(self, setup):
        o, detector, _, rebuilds, _ = setup
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()
        o.skip_prompt()
        assert o.pending_prompt is None
        # Subsequent ticks with the same source must not re-raise the prompt
        # because skipping it remembers the source for this session.
        before_rebuilds = len(rebuilds)
        o.tick()
        assert o.pending_prompt is None
        # Rebuild count should not have grown — no new prompt.
        assert len(rebuilds) == before_rebuilds

    def test_suppress_writes_state_and_notifies(self, setup):
        from app import state as state_mod
        o, detector, _, _, notifications = setup
        detector.detect_result = {"source": "webex", "url": None}
        o.tick()
        notifications.clear()
        o.suppress_source()
        assert o.pending_prompt is None
        assert "webex" in state_mod.load().get("suppressed_sources", [])
        assert notifications == [("", "webex calls will no longer be detected.")]

    def test_suppress_with_no_prompt_is_noop(self, setup):
        o, _, _, _, notifications = setup
        o.suppress_source()
        assert notifications == []

    def test_clear_prompt_drops_pending(self, setup):
        """Recovery path: a prompt queued during a now-dead recording
        must not linger into the rebuilt idle menu — clearing it keeps
        the menu from showing both 'Start Recording' and 'Record
        <source> call' right after the user sees the unexpected stop."""
        o, detector, _, _, _ = setup
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()
        assert o.pending_prompt is not None
        o.clear_prompt()
        assert o.pending_prompt is None

    def test_clear_prompt_with_no_prompt_is_noop(self, setup):
        o, _, _, _, _ = setup
        # Must not raise when called with nothing queued.
        o.clear_prompt()
        assert o.pending_prompt is None

    def test_clear_prompt_does_not_block_redetection(self, setup):
        """After a recovery-triggered clear, the next tick should be
        free to re-surface the prompt if the call is still active —
        clearing shouldn't mark the source as skipped/suppressed."""
        o, detector, _, _, _ = setup
        detector.detect_result = {"source": "zoom", "url": None}
        o.tick()
        o.clear_prompt()
        o.tick()
        assert o.pending_prompt == {"source": "zoom", "url": None}
