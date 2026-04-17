"""
Recorder — Swift binary interface for MeetingNotes.

Manages the lifecycle of ~/.bin/capture-audio:
  capture-audio start --output <path>   (SIGINT to stop gracefully)

Recordings flow:
  recordings/active/<filename>.wav  (while recording)
  recordings/queue/<filename>.wav   (after stop, ready for transcription)
"""

from __future__ import annotations

import os
import signal
import logging
import subprocess
import threading
from datetime import datetime

from . import state as state_mod
from .environment import ACTIVE_DIR, CAPTURE_AUDIO_BIN, QUEUE_DIR
from .recording_file import RecordingFile

logger = logging.getLogger(__name__)
BINARY = CAPTURE_AUDIO_BIN
LOCK_FILE = os.path.join(ACTIVE_DIR, ".lock")

# Exit code the Swift binary uses to signal "Screen Recording permission
# denied" from the check-screen-recording subcommand.
SCREEN_RECORDING_PERMISSION_DENIED = 10

_process: subprocess.Popen | None = None


def _ensure_dirs() -> None:
    os.makedirs(ACTIVE_DIR, exist_ok=True)
    os.makedirs(QUEUE_DIR, exist_ok=True)


def check_screen_recording_permission(timeout_secs: float = 8.0) -> bool:
    """Probe the capture-audio binary for Screen Recording access.

    Runs ``capture-audio check-screen-recording`` which calls
    SCShareableContent at startup. Returns True when access is granted (or
    when we can't probe and shouldn't block recording), False only on
    explicit denial (exit code 10).

    The first call triggers macOS's "grant access" dialog; subsequent calls
    after denial return immediately. Re-checking happens at each menubar
    launch — revoking the permission shows up on next start.
    """
    if not os.path.exists(BINARY):
        logger.debug(
            "capture-audio binary missing at %s; skipping permission probe",
            BINARY,
        )
        return True  # Can't probe — assume OK so we don't block the menubar.

    try:
        result = subprocess.run(
            [BINARY, "check-screen-recording"],
            capture_output=True,
            timeout=timeout_secs,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Could not probe Screen Recording permission: %s", e)
        return True

    if result.returncode == 0:
        logger.info("Screen Recording permission: granted")
        return True
    if result.returncode == SCREEN_RECORDING_PERMISSION_DENIED:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        logger.warning(
            "Screen Recording permission denied — system audio will not be "
            "captured. Grant access in System Settings → Privacy & Security "
            "→ Screen Recording. Details: %s",
            stderr,
        )
        return False
    logger.warning(
        "capture-audio check-screen-recording exited %d: %s",
        result.returncode,
        result.stderr.decode("utf-8", errors="replace").strip(),
    )
    return True  # Unknown failure — don't block recording


def _kill_stray_capture_processes(exclude_pid: int | None = None) -> int:
    """
    SIGKILL any lingering capture-audio processes owned by the current user.

    A stray process holds its ScreenCaptureKit stream open, which causes the
    next capture-audio to hang during SCK setup and record mic only. The
    .lock file only tracks the most recent PID, so earlier zombies are
    invisible to orphan detection — scan ps directly.
    """
    try:
        out = subprocess.check_output(
            ["pgrep", "-u", str(os.getuid()), "-f", BINARY],
            text=True,
        )
    except subprocess.CalledProcessError:
        return 0  # pgrep exits 1 when no matches
    except OSError as e:
        logger.warning("Could not scan for stray capture-audio processes: %s", e)
        return 0

    killed = 0
    for line in out.strip().splitlines():
        try:
            pid = int(line.strip())
        except ValueError:
            continue
        if pid == os.getpid() or pid == exclude_pid:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
            logger.warning("Killed stray capture-audio process pid=%d", pid)
            killed += 1
        except ProcessLookupError:
            pass
        except OSError as e:
            logger.warning("Could not kill stray capture-audio pid=%d: %s", pid, e)
    return killed


def start_recording(source: str) -> str:
    """
    Start recording audio for the given source.

    Generates a filename like `zoom_2026-03-31_10-02.wav`, launches the
    Swift binary, writes a .lock file, and updates state.

    Returns the path to the active recording file.
    """
    global _process

    if is_recording():
        logger.warning("start_recording() called while already recording")
        return state_mod.load().get("active_recording_path", "")

    _ensure_dirs()
    _kill_stray_capture_processes()

    now = datetime.now()
    filename = "{source}_{date}_{time}.wav".format(
        source=source.lower(),
        date=now.strftime("%Y-%m-%d"),
        time=now.strftime("%H-%M"),
    )
    recording_path = os.path.join(ACTIVE_DIR, filename)

    logger.info("Starting recording: %s", recording_path)

    try:
        _process = subprocess.Popen(
            [BINARY, "start", "--output", recording_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:
        logger.error("Failed to launch capture-audio binary: %s", e)
        raise

    # Write lock file with PID so orphan detection can find it
    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(_process.pid))
    except OSError as e:
        logger.warning("Could not write .lock file: %s", e)

    state_mod.update(
        recording_active=True,
        active_recording_path=recording_path,
        active_call_source=source,
    )

    # Monitor stderr in a background thread so capture warnings (e.g.
    # "System audio capture unavailable") appear in the log immediately.
    def _drain_stderr(proc):
        try:
            for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if line:
                    logger.warning("capture-audio: %s", line)
        except Exception:
            pass

    threading.Thread(target=_drain_stderr, args=(_process,), daemon=True).start()

    logger.info("Recording started (pid=%d) for source '%s'", _process.pid, source)
    return recording_path


def stop_recording() -> str | None:
    """
    Stop the current recording.

    Sends SIGINT; waits up to 5 seconds for clean exit, then sends SIGTERM.
    Moves the .wav from recordings/active/ to recordings/queue/.
    Updates state and removes the .lock file.

    Returns the queue path of the saved recording, or None if not recording.
    """
    global _process

    current_state = state_mod.load()
    active_path = current_state.get("active_recording_path")

    if not is_recording() or active_path is None:
        logger.warning("stop_recording() called but nothing is recording")
        # Clean up state anyway in case it's stale
        state_mod.update(
            recording_active=False,
            active_recording_path=None,
            active_call_url=None,
            active_call_source=None,
        )
        return None

    logger.info("Stopping recording (pid=%d)", _process.pid)

    try:
        _process.send_signal(signal.SIGINT)
        try:
            _process.wait(timeout=5)
            logger.info("capture-audio exited cleanly")
        except subprocess.TimeoutExpired:
            logger.warning("capture-audio did not exit after SIGINT, sending SIGTERM")
            _process.terminate()
            try:
                _process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.error("capture-audio still alive after SIGTERM, killing")
                _process.kill()
                _process.wait()
    except OSError as e:
        logger.error("Error stopping capture-audio: %s", e)
    finally:
        # Log any stderr output from the capture binary (warnings about
        # system audio capture failures, device errors, etc.)
        if _process is not None:
            try:
                _, stderr_data = _process.communicate(timeout=1)
                if stderr_data:
                    for line in stderr_data.decode("utf-8", errors="replace").strip().splitlines():
                        logger.warning("capture-audio stderr: %s", line)
            except Exception:
                pass
        _process = None

    # Remove lock file
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not remove .lock file: %s", e)

    # Move .wav + every sidecar (.sys.wav, .meta.json, etc.) to the queue dir
    # as one atomic-looking unit.
    queue_path: str | None = None
    if os.path.exists(active_path):
        try:
            moved = RecordingFile(active_path).move_to(QUEUE_DIR)
            queue_path = moved.wav_path
            logger.info("Recording moved to queue: %s", queue_path)
        except OSError as e:
            logger.error("Failed to move recording to queue: %s", e)
    else:
        logger.warning("Active recording file not found at: %s", active_path)

    state_mod.update(
        recording_active=False,
        active_recording_path=None,
        active_call_url=None,
        active_call_source=None,
    )

    return queue_path


def is_recording() -> bool:
    """Return True if the subprocess is alive."""
    if _process is None:
        return False
    return _process.poll() is None


def check_orphaned_recording() -> None:
    """
    On startup, check for a .lock file without a live process.

    If found, logs a warning and moves any orphaned .wav files from
    recordings/active/ to recordings/queue/.
    """
    _ensure_dirs()
    _kill_stray_capture_processes()

    if not os.path.exists(LOCK_FILE):
        return

    # Read PID from lock file
    orphaned_pid = None
    try:
        with open(LOCK_FILE, "r") as f:
            orphaned_pid = int(f.read().strip())
    except (OSError, ValueError) as e:
        logger.warning("Could not read .lock file: %s", e)

    # Check if that process is still alive
    process_alive = False
    if orphaned_pid is not None:
        try:
            os.kill(orphaned_pid, 0)  # signal 0 = existence check
            process_alive = True
        except ProcessLookupError:
            process_alive = False
        except PermissionError:
            # Process exists but we can't signal it
            process_alive = True

    if process_alive:
        logger.info("Lock file found with live process (pid=%d), not orphaned", orphaned_pid)
        return

    logger.warning(
        "Orphaned recording detected (lock pid=%s, process not running). "
        "Moving any active recordings to queue.",
        orphaned_pid,
    )

    # Move each orphaned recording (mic .wav plus every sidecar) to queue/.
    # We iterate only mic .wav files so RecordingFile pulls its siblings
    # along with it — previous code moved .wav files individually and
    # dropped the .meta.json sidecar.
    moved = 0
    try:
        for fname in os.listdir(ACTIVE_DIR):
            lower = fname.lower()
            if not lower.endswith(".wav") or lower.endswith(".sys.wav"):
                continue
            src = os.path.join(ACTIVE_DIR, fname)
            try:
                RecordingFile(src).move_to(QUEUE_DIR)
                logger.info("Moved orphaned recording: %s -> %s", src, QUEUE_DIR)
                moved += 1
            except OSError as e:
                logger.error("Failed to move orphaned recording %s: %s", fname, e)
    except OSError as e:
        logger.error("Could not list active recordings directory: %s", e)

    # Remove lock file
    try:
        os.remove(LOCK_FILE)
    except OSError as e:
        logger.warning("Could not remove stale .lock file: %s", e)

    # Clear stale recording state
    current_state = state_mod.load()
    if current_state.get("recording_active"):
        state_mod.update(
            recording_active=False,
            active_recording_path=None,
            active_call_url=None,
            active_call_source=None,
        )

    if moved:
        logger.info("Moved %d orphaned recording(s) to queue", moved)
