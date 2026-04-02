"""
cleanup.py — Recording auto-delete for MeetingNotes.

Deletes old recordings based on a 14-day retention policy and removes any
orphaned .wav files in the recordings queue that were never tracked in state.

Public API:
    delete_old_recordings(max_age_days) -> int
    scan_for_orphans(max_age_days)      -> int
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta

from . import state as state_mod

logger = logging.getLogger(__name__)

BASE_DIR = os.path.expanduser("~/MeetingNotes")
QUEUE_DIR = os.path.join(BASE_DIR, "recordings", "queue")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _delete_file(path: str) -> bool:
    """Delete a file if it exists. Returns True if a file was actually removed."""
    if os.path.exists(path):
        try:
            os.remove(path)
            logger.info("Deleted: %s", path)
            return True
        except OSError as e:
            logger.warning("Could not delete %s: %s", path, e)
    return False


def _delete_recording_and_sidecars(wav_path: str) -> int:
    """
    Delete a .wav file and any associated sidecar files (.meta.json, .srt).

    Returns the number of files successfully deleted.
    """
    count = 0
    base = os.path.splitext(wav_path)[0]

    for path in (wav_path, base + ".meta.json", base + ".srt"):
        if _delete_file(path):
            count += 1

    return count


def _parse_recorded_date(date_str: str) -> date | None:
    """Parse a 'YYYY-MM-DD' string into a date object."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        logger.warning("Could not parse recorded_date '%s'; skipping entry.", date_str)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def delete_old_recordings(max_age_days: int = 14) -> int:
    """
    Delete recordings in pending_deletion that are older than max_age_days.

    For each qualifying entry the .wav file, .meta.json sidecar, and .srt
    file are removed. The entry is then dropped from pending_deletion in state.

    Args:
        max_age_days: Retention period in days (default 14).

    Returns:
        Total number of files deleted.
    """
    cutoff = date.today() - timedelta(days=max_age_days)
    logger.info(
        "delete_old_recordings: cutoff=%s (max_age_days=%d)", cutoff, max_age_days
    )

    current_state = state_mod.load()
    pending: list[dict] = list(current_state.get("pending_deletion", []))

    remaining: list[dict] = []
    total_deleted = 0

    for entry in pending:
        wav_path = entry.get("path", "")
        recorded_date_str = entry.get("recorded_date", "")

        recorded_date = _parse_recorded_date(recorded_date_str)
        if recorded_date is None:
            # Keep malformed entries rather than silently dropping them
            remaining.append(entry)
            continue

        if recorded_date <= cutoff:
            logger.info(
                "Recording dated %s has exceeded %d-day retention; deleting.",
                recorded_date_str,
                max_age_days,
            )
            total_deleted += _delete_recording_and_sidecars(wav_path)
            # Entry is intentionally not added to remaining — it's been processed
        else:
            remaining.append(entry)

    state_mod.update(pending_deletion=remaining)
    logger.info(
        "delete_old_recordings complete: %d file(s) deleted, %d entry/entries remain.",
        total_deleted,
        len(remaining),
    )
    return total_deleted


def scan_for_orphans(max_age_days: int = 14) -> int:
    """
    Delete .wav files in recordings/queue/ that are not tracked in state and
    are older than max_age_days based on their filesystem modification time.

    This catches files that somehow bypassed normal pipeline tracking.

    Args:
        max_age_days: Age threshold in days (default 14).

    Returns:
        Number of orphaned files deleted.
    """
    if not os.path.isdir(QUEUE_DIR):
        logger.info("Queue directory does not exist; no orphans to scan.")
        return 0

    cutoff_ts = (
        datetime.now() - timedelta(days=max_age_days)
    ).timestamp()

    current_state = state_mod.load()
    tracked_paths: set[str] = {
        entry.get("path", "") for entry in current_state.get("pending_deletion", [])
    }

    total_deleted = 0

    try:
        entries = os.listdir(QUEUE_DIR)
    except OSError as e:
        logger.error("Could not list queue directory %s: %s", QUEUE_DIR, e)
        return 0

    for filename in entries:
        if not filename.lower().endswith(".wav"):
            continue

        wav_path = os.path.join(QUEUE_DIR, filename)

        if wav_path in tracked_paths:
            # Tracked by state — leave for delete_old_recordings to handle
            continue

        try:
            mtime = os.path.getmtime(wav_path)
        except OSError as e:
            logger.warning("Could not stat %s: %s", wav_path, e)
            continue

        if mtime <= cutoff_ts:
            logger.info(
                "Orphaned recording %s is older than %d days; deleting.",
                filename,
                max_age_days,
            )
            total_deleted += _delete_recording_and_sidecars(wav_path)

    logger.info(
        "scan_for_orphans complete: %d orphaned file(s) deleted.", total_deleted
    )
    return total_deleted
