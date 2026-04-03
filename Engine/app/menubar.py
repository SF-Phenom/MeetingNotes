"""
MeetingNotes menubar application.

Run with:
    python Engine/app/menubar.py
from ~/MeetingNotes/

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
_BASE_DIR = os.environ.get("MEETINGNOTES_HOME", os.path.expanduser("~/MeetingNotes"))
_ENGINE_DIR = os.path.join(_BASE_DIR, "Engine")
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from app import state as state_mod
from app import recorder
from app import call_detector
from app import pipeline
from app import checkin
from app import cleanup
from app import summarizer

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

_QUEUE_DIR = os.path.join(_BASE_DIR, "Engine", "recordings", "queue")

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
        self._skipped_session: str | None = None  # source string of a skipped call
        self._last_detected_source: str | None = None
        self._prompt_pending: bool = False  # guard against double-prompts
        self._transcribing: bool = False  # True while background transcription runs
        self._checkin_due: bool = False  # True when check-in threshold is reached

        # Model selection
        self._available_models: list[str] = []  # Ollama models discovered at startup
        self._discover_models()

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

        if self._transcribing:
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
        items.append(None)

        # API key status
        if os.environ.get("ANTHROPIC_API_KEY"):
            items.append(rumps.MenuItem("API Key \u2713"))
        else:
            items.append(
                rumps.MenuItem("Add API Key", callback=self._add_api_key)
            )

        items.extend([
            rumps.MenuItem("Check for Updates", callback=self._check_for_updates),
            None,
            rumps.MenuItem("Recordings ({} pending)".format(pending_count)),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ])

        self.menu = items

        # Icon priority: transcribing > check-in due > pending recordings > idle
        if self._transcribing:
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
        self.menu = [
            rumps.MenuItem("Stop Recording", callback=self._manual_stop),
            None,
            rumps.MenuItem("Recording: {} ({})".format(source, elapsed)),
            None,
            rumps.MenuItem("Quit", callback=self._quit),
        ]
        self.title = ICON_RECORDING

    # ── Timers ────────────────────────────────────────────────────────────────

    @rumps.timer(10)
    def _call_detection_tick(self, _sender) -> None:
        """Poll for active calls every 10 seconds."""
        try:
            self._run_call_detection()
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

    # ── Call detection logic ───────────────────────────────────────────────────

    def _run_call_detection(self) -> None:
        currently_recording = recorder.is_recording()
        detected = call_detector.detect_active_call()

        if detected:
            source = detected["source"]
            url = detected.get("url")
            logger.debug("Call detected: source=%s url=%s", source, url)

            # Update active call URL in state if recording
            if currently_recording:
                current_state = state_mod.load()
                if url and current_state.get("active_call_url") != url:
                    state_mod.update(active_call_url=url)

            self._last_detected_source = source

            if currently_recording:
                # Already recording — nothing more to do
                return

            # Check suppression
            current_state = state_mod.load()
            suppressed = current_state.get("suppressed_sources", [])
            if source in suppressed:
                logger.info("Source '%s' is suppressed, skipping prompt", source)
                return

            # Check if user already skipped this session
            if self._skipped_session == source:
                return

            # Don't double-prompt
            if self._prompt_pending:
                return

            self._prompt_pending = True
            try:
                self._prompt_to_record(source, url)
            finally:
                self._prompt_pending = False

        else:
            # No call detected
            if currently_recording and self._last_detected_source is not None:
                # We were recording an auto-detected call and it ended
                logger.info(
                    "Call ended for source '%s', stopping recording",
                    self._last_detected_source,
                )
                self._do_stop_recording(notify=True)
            self._last_detected_source = None
            self._skipped_session = None

    def _prompt_to_record(self, source: str, url: str | None) -> None:
        """Show a dialog asking the user whether to record the detected call."""
        logger.info("Prompting user to record: source=%s", source)

        response = rumps.alert(
            title="{} call detected".format(source),
            message="Would you like to record this meeting?",
            ok="Record",
            cancel="Skip",
            other="Never for {}".format(source),
        )

        # rumps.alert returns:
        #   1  → OK button ("Record")
        #   0  → Cancel button ("Skip")
        #  -1  → Other button ("Never for <source>")
        if response == 1:
            logger.info("User chose to record: source=%s", source)
            self._do_start_recording(source, url)
        elif response == 0:
            logger.info("User skipped recording for source=%s", source)
            self._skipped_session = source
        elif response == -1:
            logger.info("User suppressed source=%s", source)
            current_state = state_mod.load()
            suppressed = list(current_state.get("suppressed_sources", []))
            if source not in suppressed:
                suppressed.append(source)
            state_mod.update(suppressed_sources=suppressed)
            rumps.notification(
                title="MeetingNotes",
                subtitle="",
                message="{} calls will no longer be detected.".format(source),
            )

    # ── Recording actions ──────────────────────────────────────────────────────

    def _do_start_recording(self, source: str, url: str | None = None) -> None:
        """Start recording and update the UI."""
        try:
            path = recorder.start_recording(source)
            self._recording_start = time.time()
            if url:
                state_mod.update(active_call_url=url)
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
            # Auto-transcribe the recording that just finished
            if queue_path:
                self._transcribe_in_background([queue_path])
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
        if recorder.is_recording():
            logger.info("Stopping active recording before quit")
            try:
                recorder.stop_recording()
            except Exception as e:
                logger.error("Error stopping recording on quit: %s", e)
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
        self._transcribe_in_background(wav_files)

    def _transcribe_in_background(self, wav_paths: list[str]) -> None:
        """Run transcription in a background thread so the UI stays responsive."""
        if self._transcribing:
            logger.warning("Transcription already in progress, skipping")
            return

        self._transcribing = True
        self._build_idle_menu()

        def _worker():
            succeeded = 0
            failed = 0
            last_path = None
            for wav_path in wav_paths:
                try:
                    logger.info("Transcribing: %s", os.path.basename(wav_path))
                    result = pipeline.process_recording(wav_path)
                    if result:
                        succeeded += 1
                        last_path = result
                    else:
                        failed += 1
                except Exception as e:
                    logger.error("Pipeline error for %s: %s", wav_path, e, exc_info=True)
                    failed += 1

            self._transcribing = False
            # Update menu on main thread via timer (rumps is not thread-safe)
            # We set a flag and let the next timer tick rebuild the menu
            self._build_idle_menu()

            # Show completion notification
            if succeeded > 0:
                subtitle = "{} transcript{} ready".format(
                    succeeded, "s" if succeeded > 1 else ""
                )
                message = os.path.basename(last_path) if last_path else ""
                rumps.notification(
                    title="MeetingNotes",
                    subtitle=subtitle,
                    message=message,
                )
            if failed > 0:
                rumps.notification(
                    title="MeetingNotes",
                    subtitle="Transcription errors",
                    message="{} recording{} failed — check logs".format(
                        failed, "s" if failed > 1 else ""
                    ),
                )

            # Check if a check-in is now due after new transcripts
            if succeeded > 0 and checkin.should_trigger_checkin():
                rumps.notification(
                    title="MeetingNotes",
                    subtitle="Check-in ready",
                    message="You have enough new transcripts for a project check-in.",
                )

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()

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

    def _discover_models(self) -> None:
        """Discover available Ollama models on startup."""
        try:
            self._available_models = summarizer.discover_ollama_models()
            if self._available_models:
                logger.info(
                    "Ollama models available: %s", self._available_models
                )
            else:
                logger.info("No Ollama models found (Ollama may not be running)")
        except Exception as e:
            logger.warning("Model discovery failed: %s", e)
            self._available_models = []

    def _build_model_submenu(self) -> rumps.MenuItem:
        """Build the 'Model' submenu with available options."""
        current = summarizer.get_model_preference()
        submenu = rumps.MenuItem("Model")

        # Automatic option
        auto_item = rumps.MenuItem(
            "Automatic (Claude → Local)" if current == "automatic"
            else "Automatic",
            callback=self._select_model,
        )
        if current == "automatic":
            auto_item.state = 1  # checkmark
        submenu.add(auto_item)

        # Claude option
        claude_item = rumps.MenuItem("Claude", callback=self._select_model)
        if current == "claude":
            claude_item.state = 1
        submenu.add(claude_item)

        # Separator before local models
        if self._available_models:
            submenu.add(None)
            for model_name in self._available_models:
                item = rumps.MenuItem(model_name, callback=self._select_model)
                if current == model_name:
                    item.state = 1
                submenu.add(item)
        else:
            submenu.add(None)
            submenu.add(rumps.MenuItem("No local models found"))

        # Refresh option
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

        summarizer.set_model_preference(preference)
        logger.info("User selected model: %s", preference)
        self._build_idle_menu()

    def _refresh_models(self, _sender) -> None:
        """Re-scan for available Ollama models."""
        self._discover_models()
        self._build_idle_menu()
        count = len(self._available_models)
        rumps.notification(
            title="MeetingNotes",
            subtitle="Models refreshed",
            message="{} local model{} found".format(
                count, "s" if count != 1 else ""
            ),
        )

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
            return  # user cancelled

        api_key = response.text.strip()
        if not api_key:
            return

        if not api_key.startswith("sk-ant-"):
            rumps.alert(
                title="Invalid API Key",
                message="The key should start with 'sk-ant-'. Please try again.",
            )
            return

        # Save to environment for immediate use
        os.environ["ANTHROPIC_API_KEY"] = api_key

        # Persist to ~/.zshrc
        try:
            zshrc_path = os.path.expanduser("~/.zshrc")
            # Remove any existing ANTHROPIC_API_KEY line
            lines = []
            if os.path.exists(zshrc_path):
                with open(zshrc_path, "r") as f:
                    lines = [l for l in f.readlines() if "ANTHROPIC_API_KEY" not in l]
            lines.append('export ANTHROPIC_API_KEY="{}"\n'.format(api_key))
            with open(zshrc_path, "w") as f:
                f.writelines(lines)
            logger.info("API key saved to ~/.zshrc")
        except OSError as e:
            logger.error("Failed to save API key to ~/.zshrc: %s", e)

        rumps.notification(
            title="MeetingNotes",
            subtitle="API key saved",
            message="Claude summarization is now active.",
        )
        self._build_idle_menu()

    # ── Utilities ──────────────────────────────────────────────────────────────

    def _check_for_updates(self, _sender) -> None:
        """Check GitHub for updates and offer to install them."""
        threading.Thread(
            target=self._run_update_check, daemon=True
        ).start()

    def _run_update_check(self) -> None:
        """Background thread: fetch from origin and compare."""
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
            remote = sp.run(
                ["git", "-C", _BASE_DIR, "rev-parse", "origin/main"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except Exception as e:
            logger.error("Update check failed: %s", e)
            rumps.notification(
                title="MeetingNotes",
                subtitle="Update check failed",
                message="Could not reach GitHub. Check your internet connection.",
            )
            return

        if local == remote:
            rumps.notification(
                title="MeetingNotes",
                subtitle="Up to date",
                message="You're running the latest version.",
            )
            return

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
            self._apply_update()

    def _apply_update(self) -> None:
        """Pull latest changes and restart the app."""
        import subprocess as sp

        try:
            result = sp.run(
                ["git", "-C", _BASE_DIR, "pull", "--ff-only"],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode != 0:
                logger.error("git pull failed: %s", result.stderr)
                rumps.alert(
                    title="Update Failed",
                    message="Could not pull updates. Check Engine/logs/app.log for details.",
                )
                return
        except Exception as e:
            logger.error("Update pull failed: %s", e)
            rumps.alert(title="Update Failed", message=str(e))
            return

        logger.info("Update applied, restarting app")
        rumps.notification(
            title="MeetingNotes",
            subtitle="Update installed",
            message="Restarting...",
        )

        # Restart: launch a new instance then quit this one
        import sys
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
        if current_state.get("recording_active"):
            logger.warning("Stale recording_active=True found in state, clearing")
            state_mod.update(
                recording_active=False,
                active_recording_path=None,
                active_call_url=None,
                active_call_source=None,
            )
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
