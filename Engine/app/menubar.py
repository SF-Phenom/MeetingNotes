"""
MeetingNotes menubar application.

Run with:
    python Engine/app/menubar.py
from ~/MeetingNotes_RT/

Requires: rumps, psutil, anthropic
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
import threading
import time

import rumps

# Make sure the package can be imported when run directly
_BASE_DIR = os.environ.get("MEETINGNOTES_HOME", os.path.expanduser("~/MeetingNotes_RT"))
_ENGINE_DIR = os.path.join(_BASE_DIR, "Engine")
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from app import state as state_mod
from app import recorder
from app import checkin
from app import cleanup
from app.call_detector_proxy import CallDetectorProxy
from app.model_manager import ModelManager
from app.orchestrator import CallOrchestrator
from app.transcription_manager import TranscriptionManager
from app.ui_bridge import UIBridge

# ─── Logging setup ────────────────────────────────────────────────────────────

_LOG_DIR = os.path.join(_BASE_DIR, "Engine", "logs")
os.makedirs(_LOG_DIR, exist_ok=True)
_LOG_PATH = os.path.join(_LOG_DIR, "app.log")

_handler = logging.handlers.RotatingFileHandler(
    _LOG_PATH, maxBytes=5 * 1024 * 1024, backupCount=3
)
_handler.setFormatter(
    logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s")
)

logging.basicConfig(level=logging.INFO, handlers=[_handler])
logger = logging.getLogger(__name__)

# ─── Icons ────────────────────────────────────────────────────────────────────

ICON_IDLE = "🎙"
ICON_RECORDING = "🔴"
ICON_PENDING = "📝"
ICON_TRANSCRIBING = "⏳"
ICON_CHECKIN = "🔔"

# Require N consecutive "no call detected" ticks before auto-stopping.
# Each tick is 10 seconds, so 3 = 30 seconds of no detection.
CALL_END_DEBOUNCE = 3

_QUEUE_DIR = state_mod.QUEUE_DIR

# ─── MeetingNotes App ─────────────────────────────────────────────────────────


class MeetingNotesApp(rumps.App):
    def __init__(self):
        super().__init__(
            "",  # Empty title — icon only in menubar
            title=ICON_IDLE,
            quit_button=None,  # We handle Quit manually for clean shutdown
        )

        # Internal state
        self._recording_start: float | None = None
        self._checkin_due: bool = False  # True when check-in threshold is reached

        # Call-detection worker — a separate process that runs osascript/psutil
        # calls so the main process (which hosts MLX GPU threads) never forks
        # while Parakeet is running. See call_detector_proxy.py for details.
        self._call_detector = CallDetectorProxy()
        self._call_detector.start()

        # Main-thread dispatch for rumps UI updates from background threads.
        self._ui_bridge = UIBridge()

        # Owns realtime + batch transcription threads and the is_transcribing
        # flag. rumps-free by design; we inject the notification callback so
        # it doesn't need to import rumps.
        self._transcription = TranscriptionManager(
            ui_bridge=self._ui_bridge,
            rebuild_menu=self._build_idle_menu,
            notify=self._show_notification,
        )

        # Call-detection polling + record-prompt state machine. Separately
        # testable; menubar provides action callbacks so the orchestrator
        # never touches rumps directly.
        self._orchestrator = CallOrchestrator(
            call_detector=self._call_detector,
            is_recording=recorder.is_recording,
            start_recording=self._do_start_recording,
            stop_recording=self._do_stop_recording,
            rebuild_menu=self._build_idle_menu,
            notify=self._show_notification,
            debounce_ticks=CALL_END_DEBOUNCE,
        )

        # Probe Screen Recording permission once at startup. If denied, the
        # menubar surfaces a persistent item with a link to System Settings
        # instead of silently falling back to mic-only.
        self._screen_recording_ok: bool = recorder.check_screen_recording_permission()
        if not self._screen_recording_ok:
            # Queue a one-time notification — the drain timer fires once the
            # rumps runloop is active so it'll pop shortly after launch.
            self._ui_bridge.dispatch(lambda: rumps.notification(
                title="MeetingNotes",
                subtitle="System audio unavailable",
                message="Grant Screen Recording in System Settings → Privacy.",
            ))

        # Summarization model state (Ollama discovery, API key, preference).
        self._model_manager = ModelManager()
        self._model_manager.discover()

        # Build initial menu
        self._build_idle_menu()

        logger.info("MeetingNotes app started")

    # ── Menu builders ──────────────────────────────────────────────────────────

    def _build_idle_menu(self) -> None:
        """Construct the menu for the idle (not recording) state."""
        pending_count = self._count_queued_recordings()
        self._checkin_due = checkin.should_trigger_checkin()

        self.menu.clear()

        items = [
            rumps.MenuItem("Start Recording", callback=self._manual_start),
            None,  # separator
        ]

        # Non-blocking call-detected prompt (replaces the old rumps.alert
        # modal that froze the detection timer while shown).
        prompt = self._orchestrator.pending_prompt
        if prompt:
            src = prompt["source"]
            items.append(rumps.MenuItem(
                "🔴 Record {} call".format(src),
                callback=self._accept_call_prompt,
            ))
            items.append(rumps.MenuItem(
                "Skip this call",
                callback=self._skip_call_prompt,
            ))
            items.append(rumps.MenuItem(
                "Never for {}".format(src),
                callback=self._suppress_call_source,
            ))
            items.append(None)

        if self._transcription.is_transcribing:
            items.append(rumps.MenuItem("Transcribing..."))
        elif pending_count > 0:
            items.append(
                rumps.MenuItem(
                    "Transcribe All ({})".format(pending_count),
                    callback=self._transcribe_all,
                )
            )
        else:
            items.append(rumps.MenuItem("Status: Watching for calls"))

        items.append(None)

        # Check-in section
        if self._checkin_due:
            items.append(
                rumps.MenuItem(
                    "Check-in Ready — Copy Prompt",
                    callback=self._copy_checkin_prompt,
                )
            )
            items.append(
                rumps.MenuItem(
                    "Mark Check-in Complete",
                    callback=self._mark_checkin_complete,
                )
            )
            items.append(None)

        # Model selection submenu
        items.append(self._build_model_submenu())

        # Retain recordings toggle
        retain_item = rumps.MenuItem(
            "Retain Recordings",
            callback=self._toggle_retain_recordings,
        )
        retain_item.state = (
            1 if state_mod.load().get("retain_recordings", False) else 0
        )
        items.append(retain_item)
        items.append(None)

        # API key status
        if self._model_manager.api_key_present():
            items.append(rumps.MenuItem("API Key \u2713"))
        else:
            items.append(
                rumps.MenuItem("Add API Key", callback=self._add_api_key)
            )

        # Screen Recording permission status — shown only when denied so
        # the user can jump straight to System Settings to grant it.
        if not self._screen_recording_ok:
            items.append(rumps.MenuItem(
                "⚠ System audio unavailable — grant Screen Recording",
                callback=self._open_screen_recording_settings,
            ))

        items.extend([
            rumps.MenuItem("Check for Updates", callback=self._check_for_updates),
            None,
            rumps.MenuItem("Recordings ({} pending)".format(pending_count)),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ])

        self.menu = items

        # Icon priority: transcribing > check-in due > pending recordings > idle
        if self._transcription.is_transcribing:
            self.title = ICON_TRANSCRIBING
        elif self._checkin_due:
            self.title = ICON_CHECKIN
        elif pending_count > 0:
            self.title = ICON_PENDING
        else:
            self.title = ICON_IDLE

    def _build_recording_menu(self, source: str, elapsed: str) -> None:
        """Construct the menu for the recording state."""
        self.menu.clear()
        items = [
            rumps.MenuItem("Stop Recording", callback=self._manual_stop),
            None,
            rumps.MenuItem("Recording: {} ({})".format(source, elapsed)),
        ]

        # Show "View Live Transcript" if realtime transcription is active
        if self._transcription.realtime_live_transcript_path:
            items.append(
                rumps.MenuItem(
                    "View Live Transcript",
                    callback=self._open_live_transcript,
                )
            )

        items.extend([
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ])
        self.menu = items
        self.title = ICON_RECORDING

    # ── Main-thread UI dispatch ───────────────────────────────────────────────

    @rumps.timer(0.1)
    def _ui_drain_tick(self, _sender) -> None:
        """Drain all pending UI callables on the main thread."""
        self._ui_bridge.drain()

    # ── Timers ────────────────────────────────────────────────────────────────

    @rumps.timer(10)
    def _call_detection_tick(self, _sender) -> None:
        """Poll for active calls every 10 seconds. Delegates to CallOrchestrator."""
        try:
            self._orchestrator.tick()
        except Exception as e:
            logger.error("Unhandled exception in call detection timer: %s", e, exc_info=True)

    @rumps.timer(1)
    def _recording_elapsed_tick(self, _sender) -> None:
        """Update elapsed recording time in the menu every second."""
        try:
            if not recorder.is_recording():
                return
            if self._recording_start is None:
                return

            elapsed_secs = int(time.time() - self._recording_start)
            mins, secs = divmod(elapsed_secs, 60)
            elapsed_str = "{}:{:02d}".format(mins, secs)

            current_state = state_mod.load()
            source = current_state.get("active_call_source") or "unknown"

            # Update just the status line without rebuilding the whole menu
            key = "Recording: "
            for item_key in self.menu.keys():
                if item_key.startswith(key):
                    self.menu[item_key].title = "Recording: {} ({})".format(
                        source, elapsed_str
                    )
                    break
        except Exception as e:
            logger.error("Unhandled exception in elapsed timer: %s", e, exc_info=True)

    # ── Call-prompt click handlers ─────────────────────────────────────────────
    # Thin forwarders to CallOrchestrator — rumps needs the (self, sender)
    # callback signature here.

    def _accept_call_prompt(self, _sender) -> None:
        self._orchestrator.accept_prompt()

    def _skip_call_prompt(self, _sender) -> None:
        self._orchestrator.skip_prompt()

    def _suppress_call_source(self, _sender) -> None:
        self._orchestrator.suppress_source()

    # ── Recording actions ──────────────────────────────────────────────────────

    def _do_start_recording(self, source: str, url: str | None = None) -> None:
        """Start recording and update the UI."""
        try:
            path = recorder.start_recording(source)
            self._recording_start = time.time()
            if url:
                state_mod.update(active_call_url=url)

            self._transcription.start_realtime(path)

            self._build_recording_menu(source, "0:00")
            logger.info("Recording started: path=%s", path)
        except Exception as e:
            logger.error("Failed to start recording: %s", e, exc_info=True)
            rumps.alert(
                title="Recording Error",
                message="Could not start recording: {}".format(e),
            )

    def _do_stop_recording(self, notify: bool = False) -> None:
        """Stop recording and update the UI, then auto-transcribe."""
        try:
            realtime_text = self._transcription.stop_realtime()

            queue_path = recorder.stop_recording()
            self._recording_start = None
            self._build_idle_menu()
            logger.info("Recording stopped: queue_path=%s", queue_path)
            if notify and queue_path:
                rumps.notification(
                    title="MeetingNotes",
                    subtitle="Recording saved — transcribing...",
                    message=os.path.basename(queue_path),
                )
            if queue_path:
                self._transcription.submit(
                    [queue_path], pre_transcribed_text=realtime_text,
                )
        except Exception as e:
            logger.error("Failed to stop recording: %s", e, exc_info=True)

    # ── Manual menu callbacks ──────────────────────────────────────────────────

    def _manual_start(self, _sender) -> None:
        """Manual 'Start Recording' menu item."""
        if recorder.is_recording():
            return
        self._do_start_recording("manual")

    def _manual_stop(self, _sender) -> None:
        """Manual 'Stop Recording' menu item."""
        if not recorder.is_recording():
            return
        self._do_stop_recording(notify=False)

    def _quit(self, _sender) -> None:
        """Quit menu item — stop recording cleanly before exiting."""
        logger.info("Quit requested")
        try:
            self._transcription.shutdown()
        except Exception as e:
            logger.error("Error shutting down transcription manager: %s", e)
        if recorder.is_recording():
            logger.info("Stopping active recording before quit")
            try:
                recorder.stop_recording()
            except Exception as e:
                logger.error("Error stopping recording on quit: %s", e)
        try:
            self._call_detector.stop()
        except Exception as e:
            logger.error("Error stopping call detector worker: %s", e)
        logger.info("MeetingNotes app stopped")
        rumps.quit_application()

    # ── Check-in ──────────────────────────────────────────────────────────────

    def _copy_checkin_prompt(self, _sender) -> None:
        """Generate the check-in prompt and copy it to the clipboard."""
        success = checkin.copy_prompt_to_clipboard()
        if success:
            rumps.notification(
                title="MeetingNotes",
                subtitle="Check-in prompt copied to clipboard",
                message="Paste it into Claude Code to start your check-in session.",
            )
        else:
            rumps.alert(
                title="Error",
                message="Could not copy the check-in prompt to clipboard.",
            )

    def _mark_checkin_complete(self, _sender) -> None:
        """Reset the check-in counter and date."""
        checkin.mark_checkin_complete()
        self._checkin_due = False
        self._build_idle_menu()
        rumps.notification(
            title="MeetingNotes",
            subtitle="Check-in complete",
            message="Counter reset. Next check-in after 6 more transcripts or 14 days.",
        )

    # ── Transcription ─────────────────────────────────────────────────────────

    def _transcribe_all(self, _sender) -> None:
        """Transcribe all queued recordings."""
        wav_files = self._list_queued_recordings()
        if not wav_files:
            rumps.notification(
                title="MeetingNotes",
                subtitle="",
                message="No recordings to transcribe.",
            )
            return
        self._transcription.submit(wav_files)

    @staticmethod
    def _list_queued_recordings() -> list[str]:
        """Return full paths of .wav files in recordings/queue/."""
        try:
            files = sorted(
                f for f in os.listdir(_QUEUE_DIR) if f.endswith(".wav")
            )
            return [os.path.join(_QUEUE_DIR, f) for f in files]
        except OSError:
            return []

    # ── Model selection ────────────────────────────────────────────────────────

    def _build_model_submenu(self) -> rumps.MenuItem:
        """Build the 'Model' submenu from ModelManager's plain-data API."""
        current = self._model_manager.current_preference()
        available = self._model_manager.available_models()
        submenu = rumps.MenuItem("Summarization Model")

        auto_item = rumps.MenuItem(
            "Automatic (Claude → Local)" if current == "automatic"
            else "Automatic",
            callback=self._select_model,
        )
        if current == "automatic":
            auto_item.state = 1
        submenu.add(auto_item)

        claude_item = rumps.MenuItem("Claude", callback=self._select_model)
        if current == "claude":
            claude_item.state = 1
        submenu.add(claude_item)

        if available:
            submenu.add(None)
            for model_name in available:
                item = rumps.MenuItem(model_name, callback=self._select_model)
                if current == model_name:
                    item.state = 1
                submenu.add(item)
        else:
            submenu.add(None)
            submenu.add(rumps.MenuItem("No local models found"))

        submenu.add(None)
        submenu.add(
            rumps.MenuItem("Refresh Models", callback=self._refresh_models)
        )

        return submenu

    def _select_model(self, sender) -> None:
        """Handle model selection from the submenu."""
        title = sender.title
        if title.startswith("Automatic"):
            preference = "automatic"
        elif title == "Claude":
            preference = "claude"
        else:
            preference = title  # Ollama model name
        self._model_manager.set_preference(preference)
        self._build_idle_menu()

    def _refresh_models(self, _sender) -> None:
        """Re-scan for available Ollama models."""
        self._model_manager.discover()
        self._build_idle_menu()
        count = len(self._model_manager.available_models())
        rumps.notification(
            title="MeetingNotes",
            subtitle="Models refreshed",
            message="{} local model{} found".format(
                count, "s" if count != 1 else ""
            ),
        )

    # ── Live transcript viewer ────────────────────────────────────────────────

    def _open_live_transcript(self, _sender) -> None:
        """Open the live transcript file in the default text editor."""
        path = self._transcription.realtime_live_transcript_path
        if path and os.path.exists(path):
            import subprocess as sp
            sp.Popen(["open", path])

    # ── Notification helper (injected into TranscriptionManager) ──────────────

    def _show_notification(self, subtitle: str, message: str) -> None:
        """Main-thread-safe rumps.notification with MeetingNotes title."""
        rumps.notification(
            title="MeetingNotes",
            subtitle=subtitle,
            message=message,
        )

    # ── Retain recordings toggle ────────────────────────────────────────────────

    def _toggle_retain_recordings(self, _sender) -> None:
        """Toggle whether recordings are kept after transcription."""
        current = state_mod.load().get("retain_recordings", False)
        state_mod.update(retain_recordings=not current)
        self._build_idle_menu()

    # ── API Key ────────────────────────────────────────────────────────────────

    def _add_api_key(self, _sender) -> None:
        """Prompt the user to enter an Anthropic API key via a dialog."""
        window = rumps.Window(
            message="Paste your Anthropic API key (starts with sk-ant-):",
            title="Add API Key",
            default_text="",
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 24),
        )
        response = window.run()
        if not response.clicked:
            return

        ok, message = self._model_manager.save_api_key(response.text)
        if not ok:
            rumps.alert(title="Invalid API Key", message=message)
            return
        rumps.notification(
            title="MeetingNotes",
            subtitle="API key saved",
            message=message,
        )
        self._build_idle_menu()

    # ── Permissions ────────────────────────────────────────────────────────────

    def _open_screen_recording_settings(self, _sender) -> None:
        """Open the Screen Recording pane in System Settings."""
        import subprocess as sp
        sp.Popen([
            "open",
            "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        ])

    # ── Utilities ──────────────────────────────────────────────────────────────

    def _check_for_updates(self, _sender) -> None:
        """Check GitHub for updates and offer to install them."""
        threading.Thread(
            target=self._run_update_check, daemon=True
        ).start()

    def _run_update_check(self) -> None:
        """Background thread: fetch from origin and compare.

        All UI interaction is marshalled back to the main thread via
        self._ui_bridge — rumps.alert/notification from a bg thread is unsafe.
        """
        import subprocess as sp

        try:
            sp.run(
                ["git", "-C", _BASE_DIR, "fetch"],
                capture_output=True, timeout=30,
            )
            local = sp.run(
                ["git", "-C", _BASE_DIR, "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            # Get current branch name
            branch = sp.run(
                ["git", "-C", _BASE_DIR, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip() or "main"
            remote = sp.run(
                ["git", "-C", _BASE_DIR, "rev-parse", f"origin/{branch}"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception as e:
            logger.error("Update check failed: %s", e)
            self._ui_bridge.dispatch(lambda: rumps.notification(
                title="MeetingNotes",
                subtitle="Update check failed",
                message="Could not reach GitHub. Check your internet connection.",
            ))
            return

        if local == remote:
            self._ui_bridge.dispatch(lambda: rumps.notification(
                title="MeetingNotes",
                subtitle="Up to date",
                message="You're running the latest version.",
            ))
            return

        # Update available — show the install prompt on the main thread.
        self._ui_bridge.dispatch(self._prompt_update_install)

    def _prompt_update_install(self) -> None:
        """Main thread: ask the user whether to install; spawn pull on yes."""
        response = rumps.alert(
            title="Update Available",
            message=(
                "A new version of MeetingNotes is available.\n\n"
                "Install now? The app will restart automatically."
            ),
            ok="Install",
            cancel="Later",
        )
        if response == 1:  # "Install" clicked
            threading.Thread(target=self._run_update_apply, daemon=True).start()

    def _run_update_apply(self) -> None:
        """Background thread: git pull; dispatch result UI back to main."""
        import subprocess as sp

        try:
            result = sp.run(
                ["git", "-C", _BASE_DIR, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                logger.error("git pull failed: %s", result.stderr)
                self._ui_bridge.dispatch(lambda: rumps.alert(
                    title="Update Failed",
                    message="Could not pull updates. Check Engine/logs/app.log for details.",
                ))
                return
        except Exception as e:
            logger.error("Update pull failed: %s", e)
            err_msg = str(e)
            self._ui_bridge.dispatch(lambda: rumps.alert(
                title="Update Failed", message=err_msg,
            ))
            return

        logger.info("Update applied, restarting app")
        self._ui_bridge.dispatch(self._restart_after_update)

    def _restart_after_update(self) -> None:
        """Main thread: show 'installed' notification, relaunch, and quit."""
        import subprocess as sp

        rumps.notification(
            title="MeetingNotes",
            subtitle="Update installed",
            message="Restarting...",
        )
        sp.Popen(
            [sys.executable, os.path.join(_ENGINE_DIR, "app", "menubar.py")],
            start_new_session=True,
        )
        rumps.quit_application()

    @staticmethod
    def _count_queued_recordings() -> int:
        """Return the number of .wav files waiting in recordings/queue/."""
        try:
            return sum(1 for f in os.listdir(_QUEUE_DIR) if f.endswith(".wav"))
        except OSError:
            return 0


# ─── Entry point ──────────────────────────────────────────────────────────────

def main():
    logger.info("MeetingNotes starting up")

    # Check for orphaned recordings from a previous unclean shutdown
    try:
        recorder.check_orphaned_recording()
    except Exception as e:
        logger.error("Error during orphan check: %s", e, exc_info=True)

    # Ensure state file exists with clean state
    try:
        current_state = state_mod.load()
        # Clear any stale recording state on startup
        updates = {}
        if current_state.get("recording_active"):
            logger.warning("Stale recording_active=True found in state, clearing")
            updates.update(
                recording_active=False,
                active_recording_path=None,
                active_call_url=None,
                active_call_source=None,
            )
        # Remove deprecated keys from older versions
        for key in ("transcription_engine", "transcription_mode"):
            if key in current_state:
                logger.info("Removing deprecated %s from state", key)
                current_state.pop(key)
                state_mod.save(current_state)
        if updates:
            state_mod.update(**updates)
    except Exception as e:
        logger.error("Error initializing state: %s", e, exc_info=True)

    # Auto-delete old recordings (14-day retention policy)
    try:
        deleted = cleanup.delete_old_recordings()
        orphaned = cleanup.scan_for_orphans()
        if deleted or orphaned:
            logger.info(
                "Startup cleanup: %d tracked + %d orphaned files deleted",
                deleted, orphaned,
            )
    except Exception as e:
        logger.error("Error during startup cleanup: %s", e, exc_info=True)

    app = MeetingNotesApp()
    app.run()


if __name__ == "__main__":
    main()
