"""
Call detection for MeetingNotes.

Two strategies run in a single detection pass:

  Strategy A: Browser URL watching (Chrome)
    - Queries Chrome tabs via AppleScript (only when Chrome is running)
    - Detects Google Meet and Microsoft Teams URLs

  Strategy B: Native app process + window title
    - Uses psutil to find known app processes
    - Confirms via AppleScript window title check
    - Detects Zoom, Slack (Huddle), Webex, FaceTime

Returns: {"source": str, "url": str|None} or None
"""

from __future__ import annotations

import logging
import os
import subprocess

import psutil

from .environment import ZOOM_OBSERVER_BIN

logger = logging.getLogger(__name__)

# zoom-observer probe exit codes — must match ZoomObserver/Sources/main.swift.
# 0 = in meeting, 11 = Zoom running but not in meeting, 12 = Zoom not running,
# 10 = AX permission denied, anything else = unexpected failure.
_ZOOM_PROBE_TIMEOUT_SECS = 5.0
_ZOOM_PROBE_NOT_IN_MEETING = 11
_ZOOM_PROBE_NOT_RUNNING = 12

# ─── URL patterns (Strategy A) ────────────────────────────────────────────────

URL_PATTERNS = [
    # (substring, source_name)
    ("meet.google.com/", "google-meet"),
    ("teams.microsoft.com/l/meetup-join", "teams"),
    ("teams.live.com/meet/", "teams"),
]

# Zoom uses its desktop app; skip browser-based zoom
URL_EXCLUDES = [
    "app.zoom.us/wc/",
]

# ─── Native apps (Strategy B) ─────────────────────────────────────────────────

NATIVE_APPS = [
    # (process_name, window_title_keyword, source_name)
    ("zoom.us", "Meeting", "zoom"),
    ("Slack", "Huddle", "slack"),
    ("Cisco Webex Meetings", "Meeting", "webex"),
    ("FaceTime", "FaceTime", "facetime"),
]


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _run_applescript(script: str, timeout: int = 5) -> str | None:
    """Run an AppleScript snippet and return stdout, or None on failure."""
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        # AppleScript errors are not always fatal — log at debug level
        logger.debug("AppleScript returned non-zero: %s", result.stderr.strip())
        return None
    except subprocess.TimeoutExpired:
        logger.debug("AppleScript timed out")
        return None
    except OSError as e:
        logger.warning("osascript not available: %s", e)
        return None


def _process_running(name: str) -> bool:
    """Return True if a process with the given name is in the process list."""
    for proc in psutil.process_iter(["name"]):
        try:
            if proc.info["name"] == name:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


# ─── Strategy A: Chrome URL watching ─────────────────────────────────────────

def _check_chrome_urls() -> dict | None:
    """
    Query Chrome tabs via AppleScript.
    Returns {"source": str, "url": str} on match, or None.
    """
    if not _process_running("Google Chrome"):
        return None

    script = (
        'tell application "Google Chrome" '
        'to get URL of every tab of every window'
    )
    output = _run_applescript(script)
    if output is None:
        # Retry once with longer timeout — system may be under GPU load
        output = _run_applescript(script, timeout=8)
    if not output:
        return None

    # AppleScript returns something like:
    # https://mail.google.com, https://meet.google.com/abc-def, missing value
    # (comma-separated, possibly with nested braces for multiple windows)
    # Flatten to a simple list of URL strings
    raw_urls = [u.strip() for u in output.replace("{", "").replace("}", "").split(",")]

    for raw_url in raw_urls:
        url = raw_url.strip()
        if not url or url == "missing value":
            continue

        # Check exclusions first
        excluded = any(excl in url for excl in URL_EXCLUDES)
        if excluded:
            continue

        # Check patterns
        for pattern, source in URL_PATTERNS:
            if pattern in url:
                logger.debug("Chrome URL match: source=%s url=%s", source, url)
                return {"source": source, "url": url}

    return None


# ─── Strategy B: Native app process + window title ───────────────────────────

def _check_native_apps() -> dict | None:
    """
    Check psutil for known meeting app processes, then confirm via window title.
    Returns {"source": str, "url": None} on match, or None.
    """
    for process_name, title_keyword, source in NATIVE_APPS:
        if not _process_running(process_name):
            continue

        # Confirm with window title AppleScript
        script = (
            'tell application "System Events"\n'
            '    set appProc to first application process whose name is "{name}"\n'
            '    set winNames to name of every window of appProc\n'
            'end tell'
        ).format(name=process_name)

        output = _run_applescript(script)
        if output is None:
            # Process found but AppleScript failed — conservatively skip
            logger.debug(
                "Native app '%s' found but window title query failed", process_name
            )
            continue

        # output is a comma-separated list of window names
        window_names = [w.strip() for w in output.split(",")]
        for wname in window_names:
            if title_keyword.lower() in wname.lower():
                logger.debug(
                    "Native app match: source=%s window='%s'", source, wname
                )
                return {"source": source, "url": None}

    return None


# ─── Source → process name mapping (for lightweight alive check) ─────────────

_SOURCE_TO_PROCESS: dict[str, str] = {
    source: proc for proc, _kw, source in NATIVE_APPS
}
_SOURCE_TO_TITLE: dict[str, str] = {
    source: kw for _proc, kw, source in NATIVE_APPS
}
# Browser-based sources: the meeting runs inside Chrome
_BROWSER_SOURCES = {"google-meet", "teams"}

# Sources that need an Accessibility-based liveness probe instead of
# process-or-window-title heuristics. Zoom stays running after a meeting
# ends (the home screen replaces the meeting window), and the previously-
# tried window-count check false-stopped during screenshare. The AX
# probe checks for an `AXWindow` whose title contains "Meeting" — present
# throughout the call (including screenshare's floating control bar) and
# absent on the home screen.
_AX_PROBE_SOURCES = {"zoom"}


def is_call_still_active(source: str) -> bool:
    """
    Check whether the call for *source* is still in progress.

    For browser-based sources (Google Meet, Teams): only checks if Chrome
    is running. Checking URLs is unreliable during screenshare/tab switching.

    For Zoom: checks process is running AND the zoom-observer AX probe
    confirms a Meeting window is open. Falls back conservatively (still
    active) on any probe failure so a missing binary or denied AX grant
    can't trigger a false auto-stop mid-meeting.

    For other native apps (Slack, Webex, FaceTime): checks process AND
    window title keyword.
    """
    if source in _BROWSER_SOURCES:
        return _process_running("Google Chrome")

    proc_name = _SOURCE_TO_PROCESS.get(source)
    if not proc_name:
        # Unknown source (e.g. "manual") — can't check, assume still active
        return True

    if not _process_running(proc_name):
        return False

    if source in _AX_PROBE_SOURCES:
        return _zoom_in_meeting()

    title_keyword = _SOURCE_TO_TITLE.get(source)
    if title_keyword:
        return _has_meeting_window(proc_name, title_keyword)

    return True


def _zoom_in_meeting() -> bool:
    """Run the zoom-observer probe; True when Zoom has a Meeting window.

    Conservative on every failure mode: missing binary, AX denied,
    timeout, unexpected exit code → True. Only an explicit "not in
    meeting" (11) or "Zoom not running" (12) returns False. Per the
    project's overall posture, missing an auto-stop is recoverable
    (user clicks stop manually); a false stop mid-meeting truncates
    the recording and is much worse.
    """
    if not os.path.isfile(ZOOM_OBSERVER_BIN) or not os.access(
        ZOOM_OBSERVER_BIN, os.X_OK
    ):
        return True
    try:
        result = subprocess.run(
            [ZOOM_OBSERVER_BIN, "probe"],
            capture_output=True,
            timeout=_ZOOM_PROBE_TIMEOUT_SECS,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.debug("Zoom probe failed (%s); assuming still in meeting.", e)
        return True
    if result.returncode == 0:
        return True
    if result.returncode in (_ZOOM_PROBE_NOT_IN_MEETING, _ZOOM_PROBE_NOT_RUNNING):
        return False
    logger.debug(
        "Zoom probe exited %d; assuming still in meeting.", result.returncode
    )
    return True


def _window_count(process_name: str) -> int:
    """Return the number of windows for a given app process."""
    script = (
        'tell application "System Events"\n'
        '    set appProc to first application process whose name is "{name}"\n'
        '    return count of windows of appProc\n'
        'end tell'
    ).format(name=process_name)

    output = _run_applescript(script)
    if output is None:
        # AppleScript failed — return high count to avoid false stop
        return 99

    try:
        return int(output)
    except ValueError:
        logger.debug("Unexpected window count output for %s: %r", process_name, output)
        return 99


def _has_meeting_window(process_name: str, title_keyword: str) -> bool:
    """Check if an app has a window whose title contains the keyword."""
    script = (
        'tell application "System Events"\n'
        '    set appProc to first application process whose name is "{name}"\n'
        '    set winNames to name of every window of appProc\n'
        'end tell'
    ).format(name=process_name)

    output = _run_applescript(script)
    if output is None:
        # AppleScript failed — conservatively assume still in call
        return True

    window_names = [w.strip() for w in output.split(",")]
    for wname in window_names:
        if title_keyword.lower() in wname.lower():
            return True
    return False


# ─── Main detection function ──────────────────────────────────────────────────

def detect_active_call() -> dict | None:
    """
    Run both detection strategies and return the first match found.

    Returns {"source": str, "url": str|None} or None if no call detected.
    Designed to be called on a ~10 second timer.
    """
    # Strategy A: browser URLs (Chrome)
    result = _check_chrome_urls()
    if result:
        return result

    # Strategy B: native apps
    result = _check_native_apps()
    if result:
        return result

    return None
