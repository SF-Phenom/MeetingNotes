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
import subprocess

import psutil

logger = logging.getLogger(__name__)

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

def _run_applescript(script: str, timeout: int = 3) -> str | None:
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
