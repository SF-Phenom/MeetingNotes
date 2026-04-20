"""
State management for MeetingNotes.
Manages Engine/state.json with atomic, file-locked writes.

Two complementary APIs:

  Dict API (original):
    ``load()`` returns a dict, ``update(**kwargs)`` merges keyword args.
    Still used by most existing callers.

  Typed API (``State`` dataclass):
    ``State.load()`` returns a frozen dataclass with attribute access.
    Preferred for new code — the schema is checked at definition time
    and IDEs can autocomplete field names. Both APIs write to the same
    state.json and either may be used interchangeably. See the ``State``
    docstring for the authoritative field list.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from dataclasses import dataclass, field, fields

from app.environment import ENGINE_DIR

logger = logging.getLogger(__name__)

STATE_PATH = os.path.join(ENGINE_DIR, "state.json")

@dataclass(frozen=True)
class State:
    """Typed view of state.json. Frozen — use update() to mutate on disk.

    Field list is the authoritative schema. Defaults here mirror
    ``DEFAULT_STATE`` below so the two can't drift.
    """

    transcripts_since_checkin: int = 0
    last_checkin_date: str | None = None
    suppressed_sources: list[str] = field(default_factory=list)
    pending_deletion: list[dict] = field(default_factory=list)
    recording_active: bool = False
    active_recording_path: str | None = None
    active_call_url: str | None = None
    active_call_source: str | None = None
    # User-selected transcription backend. "parakeet" (default) or
    # "apple_speech". Read by transcription_engine.get_*_engine factories.
    transcription_engine: str = "parakeet"
    # Action-item exporter — "disabled" (default) or "apple_reminders".
    # Read by exporter.export_action_items at the end of the pipeline.
    exporter_backend: str = "disabled"
    # Reminders list name when exporter_backend == "apple_reminders".
    # Created on first export if it doesn't exist.
    apple_reminders_list: str = "MeetingNotes"
    # Experimental speaker-diarization toggle. When True AND a diarizer
    # backend is available (see app/diarizer.py), the batch pipeline
    # assigns Speaker A / Speaker B / … labels to paragraphs in the
    # post-record .md transcript. Defaults off; intentionally not exposed
    # in the menubar yet — flipped via direct state.json edit or the
    # MEETINGNOTES_DIARIZATION env var until the feature is ready for a
    # visible toggle.
    diarization_enabled: bool = False

    @classmethod
    def from_raw(cls, raw: dict) -> "State":
        """Build a State from an arbitrary dict, using defaults for missing
        fields and silently ignoring unknown ones (keeps forward-compat with
        state files written by future versions)."""
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in raw.items() if k in known}
        return cls(**kwargs)

    @classmethod
    def load(cls) -> "State":
        """Read state.json and return a typed State."""
        return cls.from_raw(load())


# Dict of default values — kept in sync with State's field defaults by the
# test suite. Historical callers use ``load().get(key, default)`` patterns,
# so this continues to exist for back-compat.
DEFAULT_STATE = {
    "transcripts_since_checkin": 0,
    "last_checkin_date": None,
    "suppressed_sources": [],
    "pending_deletion": [],
    "recording_active": False,
    "active_recording_path": None,
    "active_call_url": None,
    "active_call_source": None,
    "transcription_engine": "parakeet",
    "exporter_backend": "disabled",
    "apple_reminders_list": "MeetingNotes",
    "diarization_enabled": False,
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
    """Write state atomically with file locking: lock, write .tmp, fsync, rename."""
    lock_path = STATE_PATH + ".lock"
    tmp_path = STATE_PATH + ".tmp"
    try:
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                with open(tmp_path, "w") as f:
                    json.dump(state, f, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
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
                # Write directly (we already hold the lock). fsync before
                # rename so a crash mid-rename can't leave a half-written
                # state.json visible after recovery.
                tmp_path = STATE_PATH + ".tmp"
                with open(tmp_path, "w") as f:
                    json.dump(state, f, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(tmp_path, STATE_PATH)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        return state
    except OSError as e:
        logger.error("Failed to update state.json: %s", e)
        raise
