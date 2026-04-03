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
from datetime import datetime

from . import state as state_mod
from .state import BASE_DIR

logger = logging.getLogger(__name__)
BINARY = os.path.join(BASE_DIR, "Engine", ".bin", "capture-audio")
ACTIVE_DIR = os.path.join(BASE_DIR, "Engine", "recordings", "active")
QUEUE_DIR = os.path.join(BASE_DIR, "Engine", "recordings", "queue")
LOCK_FILE = os.path.join(BASE_DIR, "Engine", "recordings", "active", ".lock")

_process: subprocess.Popen | None = None


def _ensure_dirs() -> None:
    os.makedirs(ACTIVE_DIR, exist_ok=True)
    os.makedirs(QUEUE_DIR, exist_ok=True)


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
        _process = None

    # Remove lock file
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not remove .lock file: %s", e)

    # Move recording to queue
    queue_path = None
    if os.path.exists(active_path):
        filename = os.path.basename(active_path)
        queue_path = os.path.join(QUEUE_DIR, filename)
        try:
            os.rename(active_path, queue_path)
            logger.info("Recording moved to queue: %s", queue_path)
        except OSError as e:
            logger.error("Failed to move recording to queue: %s", e)
            queue_path = None
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

    # Move all .wav files from active/ to queue/
    moved = 0
    try:
        for fname in os.listdir(ACTIVE_DIR):
            if fname.endswith(".wav"):
                src = os.path.join(ACTIVE_DIR, fname)
                dst = os.path.join(QUEUE_DIR, fname)
                try:
                    os.rename(src, dst)
                    logger.info("Moved orphaned recording: %s -> %s", src, dst)
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
