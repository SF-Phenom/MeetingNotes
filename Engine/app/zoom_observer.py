"""Zoom Accessibility observer — subprocess lifecycle from Python.

Wraps ``Engine/.bin/zoom-observer`` (built from ``Engine/ZoomObserver/``).
The observer runs alongside ``capture-audio`` during Zoom recordings when
the user has opted in via ``state.ax_participants_enabled`` (or the
``MEETINGNOTES_AX_PARTICIPANTS`` env override) AND macOS Accessibility
permission has been granted to the zoom-observer binary.

Lifecycle mirrors ``recorder.py``'s capture-audio pattern:

    start_observer(wav_path)  →  Popen, write .zoom-observer.lock
    stop_observer()           →  SIGINT, wait 5s, SIGTERM, SIGKILL

The observer writes ``<base>.participants.jsonl`` next to the .wav. That
sidecar is picked up by ``RecordingFile`` during the active → queue →
done moves and consumed by the pipeline post-recording.

This module is deliberately narrow — it knows nothing about diarization
bounds. Consumers (pipeline.py) handle the sidecar parsing themselves.
Failure to launch or observe is ALWAYS silent: a Zoom call should never
fail because the experimental AX feature had a bad day.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess

from . import state as state_mod
from .environment import ACTIVE_DIR, ZOOM_OBSERVER_BIN

logger = logging.getLogger(__name__)

# Env var override for state.ax_participants_enabled. Accepts the usual
# truthy strings case-insensitively; anything else (including unset)
# defers to state.json. Mirrors DIARIZATION_ENABLED_ENV_VAR in pipeline.py.
AX_PARTICIPANTS_ENABLED_ENV_VAR = "MEETINGNOTES_AX_PARTICIPANTS"

# Exit code the Swift binary uses to signal "Accessibility permission not
# granted" from the check-accessibility subcommand. Mirrors
# AUDIO_CAPTURE_PERMISSION_DENIED in recorder.py.
ACCESSIBILITY_PERMISSION_DENIED = 10

# Per-observer lockfile. Kept distinct from capture-audio's .lock so
# orphan recovery can detect a stranded observer independent of the
# audio recorder.
LOCK_FILE = os.path.join(ACTIVE_DIR, ".zoom-observer.lock")

# Pipe-buffer for stderr drain thread; capped so a noisy observer can't
# exhaust memory via unbounded stderr spam.
_STDERR_LINE_BYTES_LIMIT = 1024


def is_available() -> bool:
    """True when the zoom-observer binary is present and executable."""
    return os.path.isfile(ZOOM_OBSERVER_BIN) and os.access(ZOOM_OBSERVER_BIN, os.X_OK)


def ax_participants_enabled() -> bool:
    """Resolve the AX participant observer flag.

    Precedence: ``MEETINGNOTES_AX_PARTICIPANTS`` env > state.json >
    default ``False``. Failure to read state.json is logged and treated
    as ``False`` — the observer is experimental and must never force
    itself on users via a state-load failure.
    """
    env = os.environ.get(AX_PARTICIPANTS_ENABLED_ENV_VAR, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        return state_mod.State.load().ax_participants_enabled
    except Exception as e:  # noqa: BLE001 — never break recording for a feature flag
        logger.warning(
            "Could not read ax_participants_enabled from state (%s); treating as off.",
            e,
        )
        return False


def check_accessibility_permission(timeout_secs: float = 8.0) -> bool:
    """Probe the zoom-observer binary for Accessibility access.

    Runs ``zoom-observer check-accessibility`` which calls
    ``AXIsProcessTrustedWithOptions`` with the prompt flag enabled —
    so on first call macOS shows the "grant access" dialog. Returns
    True when trusted, False only on explicit denial (exit 10). On any
    other failure (binary missing, timeout, OS error) returns True so
    the caller doesn't block on the permission check itself; the start
    path handles the actual "not trusted" case on its own.
    """
    if not is_available():
        logger.debug(
            "zoom-observer binary missing at %s; skipping permission probe",
            ZOOM_OBSERVER_BIN,
        )
        return True

    try:
        result = subprocess.run(
            [ZOOM_OBSERVER_BIN, "check-accessibility"],
            capture_output=True,
            timeout=timeout_secs,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("Could not probe Accessibility permission: %s", e)
        return True

    if result.returncode == 0:
        logger.info("Accessibility permission: granted")
        return True
    if result.returncode == ACCESSIBILITY_PERMISSION_DENIED:
        logger.warning(
            "Accessibility permission denied — Zoom participant count "
            "observer will not run. Grant access in System Settings → "
            "Privacy & Security → Accessibility.",
        )
        return False
    logger.warning(
        "zoom-observer check-accessibility exited %d: %s",
        result.returncode,
        result.stderr.decode("utf-8", errors="replace").strip(),
    )
    return True


def _sidecar_path_for(wav_path: str) -> str:
    """Derive the .participants.jsonl path from a .wav path."""
    if wav_path.endswith(".wav"):
        base = wav_path[:-4]
    else:
        base = os.path.splitext(wav_path)[0]
    return base + ".participants.jsonl"


def start_observer(wav_path: str) -> subprocess.Popen | None:
    """Launch the observer for ``wav_path`` if conditions are met.

    Returns the Popen handle on success, or None when the observer
    should not or cannot run. Silent failures (binary missing, flag
    off, OS error) produce a None return and an info/warning log line;
    the caller must tolerate a None handle (the feature is optional).
    """
    if not ax_participants_enabled():
        logger.debug("AX participants observer disabled; skipping.")
        return None
    if not is_available():
        logger.info(
            "AX participants observer enabled but binary missing at %s; "
            "run setup.command to build it. Continuing without it.",
            ZOOM_OBSERVER_BIN,
        )
        return None

    os.makedirs(ACTIVE_DIR, exist_ok=True)
    sidecar_path = _sidecar_path_for(wav_path)

    try:
        proc = subprocess.Popen(
            [ZOOM_OBSERVER_BIN, "start", "--output", sidecar_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:
        logger.warning("Failed to launch zoom-observer: %s", e)
        return None

    try:
        with open(LOCK_FILE, "w") as f:
            f.write(str(proc.pid))
    except OSError as e:
        logger.warning("Could not write zoom-observer .lock file: %s", e)

    # Drain stderr in a background thread so AX warnings (permission
    # denied, tree-dump on first attach, Zoom-not-running notices) surface
    # in the app log immediately instead of blocking on pipe capacity.
    import threading

    def _drain_stderr(p: subprocess.Popen) -> None:
        try:
            for raw in p.stderr:  # type: ignore[union-attr]
                line = raw[:_STDERR_LINE_BYTES_LIMIT].decode(
                    "utf-8", errors="replace"
                ).strip()
                if line:
                    logger.info("zoom-observer: %s", line)
        except Exception:  # noqa: BLE001 — drain must never leak to caller
            pass

    threading.Thread(target=_drain_stderr, args=(proc,), daemon=True).start()

    logger.info(
        "Zoom AX observer started (pid=%d) → %s",
        proc.pid, os.path.basename(sidecar_path),
    )
    return proc


def stop_observer(proc: subprocess.Popen | None) -> None:
    """Stop the observer subprocess gracefully.

    SIGINT → wait 5s → SIGTERM → wait 3s → SIGKILL. Always removes the
    lockfile. No-op when ``proc`` is None (observer was never launched).
    """
    if proc is None:
        _remove_lockfile()
        return

    if proc.poll() is not None:
        # Already exited on its own.
        _remove_lockfile()
        return

    logger.info("Stopping zoom-observer (pid=%d)", proc.pid)
    try:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning("zoom-observer did not exit after SIGINT; sending SIGTERM")
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.error("zoom-observer still alive after SIGTERM; killing")
                proc.kill()
                proc.wait()
    except OSError as e:
        logger.warning("Error stopping zoom-observer: %s", e)
    finally:
        _remove_lockfile()


def _remove_lockfile() -> None:
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not remove zoom-observer .lock: %s", e)


def recover_orphan() -> None:
    """Detect and clean up a stranded observer from a prior app crash.

    Called at menubar startup alongside ``recorder.check_orphaned_recording``.
    If the lock file references a PID that's no longer alive, remove
    the lock; any partial JSONL sidecar gets picked up by the normal
    active → queue move the audio recorder's orphan recovery triggers.
    """
    if not os.path.exists(LOCK_FILE):
        return
    orphaned_pid: int | None = None
    try:
        with open(LOCK_FILE, "r") as f:
            orphaned_pid = int(f.read().strip())
    except (OSError, ValueError) as e:
        logger.warning("Could not read zoom-observer .lock: %s", e)

    if orphaned_pid is not None:
        try:
            os.kill(orphaned_pid, 0)  # existence check
            logger.info(
                "zoom-observer .lock references live pid=%d; not orphaned",
                orphaned_pid,
            )
            return
        except ProcessLookupError:
            pass
        except PermissionError:
            # Process exists but we can't signal it — leave the lock alone.
            return

    logger.warning(
        "Orphaned zoom-observer detected (lock pid=%s). Removing lock.",
        orphaned_pid,
    )
    _remove_lockfile()
