"""
State management for MeetingNotes.
Manages Engine/state.json with atomic, file-locked writes.

state.json schema:
{
  "transcripts_since_checkin": int,
  "last_checkin_date": str|null,
  "suppressed_sources": [str],
  "pending_deletion": [{"path": str, "recorded_date": str}],
  "retain_recordings": bool,
  "recording_active": bool,
  "active_recording_path": str|null,
  "active_call_url": str|null,
  "active_call_source": str|null,
  "transcription_mode": str       # "live" | "batch"
}
"""

import fcntl
import json
import os
import logging

logger = logging.getLogger(__name__)

BASE_DIR = os.environ.get("MEETINGNOTES_HOME", os.path.expanduser("~/MeetingNotes_RT"))
STATE_PATH = os.path.join(BASE_DIR, "Engine", "state.json")

# Shared path constants — import these instead of redefining per module
ENGINE_DIR = os.path.join(BASE_DIR, "Engine")
QUEUE_DIR = os.path.join(ENGINE_DIR, "recordings", "queue")
ACTIVE_DIR = os.path.join(ENGINE_DIR, "recordings", "active")
DONE_DIR = os.path.join(ENGINE_DIR, "recordings", "done")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
CONTEXT_PATH = os.path.join(BASE_DIR, "Settings", "context.md")

DEFAULT_STATE = {
    "transcripts_since_checkin": 0,
    "last_checkin_date": None,
    "suppressed_sources": [],
    "pending_deletion": [],
    "retain_recordings": False,
    "recording_active": False,
    "active_recording_path": None,
    "active_call_url": None,
    "active_call_source": None,
    "transcription_mode": "live",       # "live" (parakeet) | "batch" (whisper)
}


def load() -> dict:
    """Read the state file. Returns default state if missing or corrupt."""
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
        # Merge with defaults so new keys are always present
        merged = dict(DEFAULT_STATE)
        merged.update(data)
        return merged
    except FileNotFoundError:
        logger.info("state.json not found, using default state")
        return dict(DEFAULT_STATE)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read state.json (%s), using default state", e)
        return dict(DEFAULT_STATE)


def save(state: dict) -> None:
    """Write state atomically with file locking: lock, write .tmp, rename."""
    lock_path = STATE_PATH + ".lock"
    tmp_path = STATE_PATH + ".tmp"
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                with open(tmp_path, "w") as f:
                    json.dump(state, f, indent=2)
                    f.write("\n")
                os.rename(tmp_path, STATE_PATH)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except OSError as e:
        logger.error("Failed to save state.json: %s", e)
        raise


def update(**kwargs) -> dict:
    """Load state, merge kwargs, save, and return the updated state.

    Uses file locking to prevent concurrent read-modify-write races
    between the main thread and background transcription threads.
    """
    lock_path = STATE_PATH + ".lock"
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                state = load()
                state.update(kwargs)
                # Write directly (we already hold the lock)
                tmp_path = STATE_PATH + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(state, f, indent=2)
                    f.write("\n")
                os.rename(tmp_path, STATE_PATH)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        return state
    except OSError as e:
        logger.error("Failed to update state.json: %s", e)
        raise
