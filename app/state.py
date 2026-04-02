"""
State management for MeetingNotes.
Manages ~/MeetingNotes/state.json with atomic writes.

state.json schema:
{
  "transcripts_since_checkin": int,
  "last_checkin_date": str|null,
  "suppressed_sources": [str],
  "pending_deletion": [{"path": str, "recorded_date": str}],
  "recording_active": bool,
  "active_recording_path": str|null,
  "active_call_url": str|null,
  "active_call_source": str|null
}
"""

import json
import os
import logging

logger = logging.getLogger(__name__)

BASE_DIR = os.path.expanduser("~/MeetingNotes")
STATE_PATH = os.path.join(BASE_DIR, "state.json")

DEFAULT_STATE = {
    "transcripts_since_checkin": 0,
    "last_checkin_date": None,
    "suppressed_sources": [],
    "pending_deletion": [],
    "recording_active": False,
    "active_recording_path": None,
    "active_call_url": None,
    "active_call_source": None,
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
    """Write state atomically: write to .tmp then os.rename()."""
    tmp_path = STATE_PATH + ".tmp"
    try:
        with open(tmp_path, "w") as f:
            json.dump(state, f, indent=2)
            f.write("\n")
        os.rename(tmp_path, STATE_PATH)
    except OSError as e:
        logger.error("Failed to save state.json: %s", e)
        raise


def update(**kwargs) -> dict:
    """Load state, merge kwargs, save, and return the updated state."""
    state = load()
    state.update(kwargs)
    save(state)
    return state
