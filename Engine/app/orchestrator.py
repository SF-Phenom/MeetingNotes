"""CallOrchestrator — call-detection polling + record-prompt state machine.

Owns the "is there a call right now, and if so what should we offer the
user?" logic that used to sit inside MeetingNotesApp. Every 10s the
menubar calls ``tick()`` which:

  - While recording: asks the detector whether the call app/window is
    still alive. After ``debounce_ticks`` consecutive misses, auto-stops
    the recording.

  - While idle: asks the detector whether a recordable call has started.
    If yes, sets up a non-blocking menu prompt (replaces the old
    rumps.alert modal that used to freeze the 10s timer) and posts a
    notification so the user notices the menubar icon.

The prompt state ``pending_prompt`` is consumed by the menubar when it
builds the menu. The three click handlers — accept / skip / suppress —
live here so the state machine is all in one place.

rumps-free. Menubar injects the callbacks this needs:
  - ``is_recording`` / ``start_recording`` / ``stop_recording`` — recording actions
  - ``rebuild_menu`` — tell the menubar to redraw
  - ``notify(subtitle, message)`` — show a rumps notification (main-thread-safe)
"""
from __future__ import annotations

import logging
from typing import Callable

from app import state as state_mod
from app.call_detector_proxy import CallDetectorProxy

logger = logging.getLogger(__name__)

IsRecordingFn = Callable[[], bool]
StartRecordingFn = Callable[[str, "str | None"], None]
StopRecordingFn = Callable[[bool], None]
RebuildMenuFn = Callable[[], None]
NotifyFn = Callable[[str, str], None]


class CallOrchestrator:
    """Call-detection state machine + record-prompt handlers."""

    def __init__(
        self,
        *,
        call_detector: CallDetectorProxy,
        is_recording: IsRecordingFn,
        start_recording: StartRecordingFn,
        stop_recording: StopRecordingFn,
        rebuild_menu: RebuildMenuFn,
        notify: NotifyFn,
        debounce_ticks: int = 3,
    ) -> None:
        self._call_detector = call_detector
        self._is_recording = is_recording
        self._start_recording = start_recording
        self._stop_recording = stop_recording
        self._rebuild_menu = rebuild_menu
        self._notify = notify
        self._debounce_ticks = debounce_ticks

        self._last_detected_source: str | None = None
        self._skipped_session: str | None = None
        self._no_call_count: int = 0
        self._pending_prompt: dict | None = None

    # -- Read-only state used by the menubar ---------------------------------

    @property
    def pending_prompt(self) -> dict | None:
        """The active record-this-call prompt, or None."""
        return self._pending_prompt

    # -- Tick driver ---------------------------------------------------------

    def tick(self) -> None:
        """One poll step. Called by the 10s rumps timer on the main thread."""
        if self._is_recording() and self._last_detected_source is not None:
            self._tick_while_recording()
            return
        self._tick_while_idle()

    def _tick_while_recording(self) -> None:
        """Poll whether the call is still active; auto-stop on debounce."""
        still_alive = self._call_detector.is_call_still_active(
            self._last_detected_source
        )
        if still_alive:
            self._no_call_count = 0
            return

        self._no_call_count += 1
        if self._no_call_count < self._debounce_ticks:
            logger.debug(
                "Call app not detected (%d/%d), waiting for debounce",
                self._no_call_count, self._debounce_ticks,
            )
            return

        logger.info(
            "Call app '%s' no longer running (%d consecutive misses), "
            "stopping recording",
            self._last_detected_source, self._no_call_count,
        )
        self._stop_recording(True)
        self._no_call_count = 0
        self._last_detected_source = None
        self._skipped_session = None

    def _tick_while_idle(self) -> None:
        """Poll for a new call; raise the record-this-call prompt."""
        detected = self._call_detector.detect_active_call()
        if not detected:
            # Nothing running — clear any stale prompt state.
            self._last_detected_source = None
            self._skipped_session = None
            if self._pending_prompt is not None:
                logger.debug("Clearing stale call prompt — no call detected")
                self._pending_prompt = None
                self._rebuild_menu()
            return

        source = detected["source"]
        url = detected.get("url")
        logger.debug("Call detected: source=%s url=%s", source, url)
        self._last_detected_source = source

        suppressed = state_mod.load().get("suppressed_sources", [])
        if source in suppressed:
            logger.info("Source '%s' is suppressed, skipping prompt", source)
            return

        if self._skipped_session == source:
            return

        # Already prompting for this source — keep quiet on subsequent ticks.
        if (
            self._pending_prompt is not None
            and self._pending_prompt.get("source") == source
        ):
            return

        logger.info("Call detected, offering record prompt: source=%s", source)
        self._pending_prompt = {"source": source, "url": url}
        self._rebuild_menu()
        self._notify(
            "{} call detected".format(source),
            "Click the menubar icon to record.",
        )

    # -- Prompt click handlers ------------------------------------------------

    def accept_prompt(self) -> None:
        """User clicked 'Record <Source> call' — start the recording."""
        if self._pending_prompt is None:
            return
        prompt = self._pending_prompt
        self._pending_prompt = None
        logger.info("User accepted call prompt: source=%s", prompt["source"])
        self._start_recording(prompt["source"], prompt.get("url"))

    def skip_prompt(self) -> None:
        """User clicked 'Skip this call'."""
        if self._pending_prompt is None:
            return
        source = self._pending_prompt["source"]
        self._pending_prompt = None
        self._skipped_session = source
        logger.info("User skipped call prompt: source=%s", source)
        self._rebuild_menu()

    def clear_prompt(self) -> None:
        """Drop any pending record-this-call prompt.

        Called from the recovery path when capture-audio dies
        unexpectedly — otherwise the idle menu rebuilds with BOTH the
        generic "Start Recording" entry AND the stale "Record <source>
        call" prompt (the prompt was queued while the old recording was
        live). The next 10s tick re-surfaces the prompt if the call is
        still active, so the user isn't stranded — just given a beat to
        notice what happened.
        """
        if self._pending_prompt is None:
            return
        source = self._pending_prompt.get("source")
        self._pending_prompt = None
        logger.info("Cleared pending call prompt (source=%s)", source)

    def suppress_source(self) -> None:
        """User clicked 'Never for <Source>'."""
        if self._pending_prompt is None:
            return
        source = self._pending_prompt["source"]
        self._pending_prompt = None
        current_state = state_mod.load()
        suppressed = list(current_state.get("suppressed_sources", []))
        if source not in suppressed:
            suppressed.append(source)
        state_mod.update(suppressed_sources=suppressed)
        logger.info("User suppressed call source: %s", source)
        self._rebuild_menu()
        self._notify("", "{} calls will no longer be detected.".format(source))
