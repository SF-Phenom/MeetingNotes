"""
calendar_lookup.py — Google Calendar integration for MeetingNotes.

Looks up a meeting on the user's Google Calendar that matches a recording's
time window, returning the meeting title and participant list.

Public API:
    lookup_meeting(date_str, time_str, duration_minutes) -> dict | None
    enrich_metadata(wav_path) -> dict
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.environment import BASE_DIR

logger = logging.getLogger(__name__)
CREDENTIALS_DIR = os.path.join(BASE_DIR, "Engine", ".credentials")
CLIENT_SECRET_PATH = os.path.join(CREDENTIALS_DIR, "google_oauth_client.json")
TOKEN_PATH = os.path.join(CREDENTIALS_DIR, "google_token.json")

SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]


def _local_zoneinfo() -> ZoneInfo | None:
    """Resolve the system IANA timezone via the /etc/localtime symlink.

    Returns a ZoneInfo (DST-aware) or None if the symlink can't be parsed
    — callers should fall back to ``datetime.astimezone()`` with no arg,
    which uses the same underlying tz database via libc.
    """
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return None
    marker = "zoneinfo/"
    idx = target.find(marker)
    if idx < 0:
        return None
    try:
        return ZoneInfo(target[idx + len(marker):])
    except Exception:  # noqa: BLE001 — ZoneInfoNotFoundError + others
        return None


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _get_credentials(*, interactive: bool = False):
    """
    Load saved credentials, refreshing if needed.

    When ``interactive`` is False (the default — pipeline path), a missing
    or unrefreshable token causes this to return None rather than launch
    the OAuth consent flow. ``flow.run_local_server`` blocks the calling
    thread until the user completes the browser dance, which is fine for
    a menu-triggered action but would wedge the transcription worker if
    fired automatically. The user opts in via the "Sign in to Google
    Calendar" menu item, which calls this with ``interactive=True``.

    Returns a valid Credentials object, or None on any failure.
    """
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        logger.warning(
            "Google API libraries not installed. "
            "Run: pip install google-auth-oauthlib google-api-python-client"
        )
        return None

    creds = None

    # Load saved token if it exists. Broad catch: google.auth raises
    # ValueError, json.JSONDecodeError, and its own auth exceptions depending
    # on corruption mode. Any of them mean the same thing for us — re-auth.
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception as e:  # noqa: BLE001 — all failures mean re-auth
            logger.warning("Could not load saved token (%s); will re-authenticate.", e)
            creds = None

    if creds and creds.valid:
        return creds

    # Token refresh is non-interactive (network call only) — safe to run
    # on the pipeline path.
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception as e:  # noqa: BLE001 — google.auth raises many types
            logger.warning(
                "Token refresh failed (%s); deleting stale token.", e
            )
            try:
                os.remove(TOKEN_PATH)
            except OSError:
                pass
            creds = None

    # Below this point we'd need to launch the OAuth consent flow, which
    # blocks. Bail out unless the caller explicitly opted in.
    if not interactive:
        logger.info(
            "No valid Google Calendar token; sign in via the menubar to enable enrichment."
        )
        return None

    if not os.path.exists(CLIENT_SECRET_PATH):
        logger.error(
            "Google OAuth client secret not found at %s. "
            "Calendar lookup is unavailable.",
            CLIENT_SECRET_PATH,
        )
        return None

    try:
        flow = InstalledAppFlow.from_client_secrets_file(CLIENT_SECRET_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
        _save_token(creds)
        return creds
    except Exception as e:  # noqa: BLE001 — OAuth errors are diverse; disable on any
        logger.error("OAuth flow failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Authorization status (for the menubar)
# ---------------------------------------------------------------------------

def is_authorized() -> bool:
    """Cheap check: do we already have a token file on disk?

    Doesn't validate the token — that requires a network round-trip. The
    pipeline path will discover validity (or refresh) the next time it
    runs ``_get_credentials``. This is intended only for the menubar to
    decide which label to show ("Sign in to Google Calendar" vs.
    "Calendar Connected ✓").
    """
    return os.path.exists(TOKEN_PATH)


def authorize_interactive() -> tuple[bool, str]:
    """Run the OAuth flow and return (success, user-facing message).

    Safe to call from a background thread — ``flow.run_local_server``
    blocks for as long as the browser dance takes, so don't call this on
    the main rumps thread.
    """
    if not os.path.exists(CLIENT_SECRET_PATH):
        return False, (
            f"OAuth client secret not found at {CLIENT_SECRET_PATH}. "
            "See README for setup instructions."
        )
    creds = _get_credentials(interactive=True)
    if creds is None:
        return False, "Authorization failed — see logs for details."
    return True, "Calendar enrichment is now active for new recordings."


def _save_token(creds) -> None:
    """Persist credentials to the token file."""
    try:
        os.makedirs(CREDENTIALS_DIR, exist_ok=True)
        with open(TOKEN_PATH, "w", encoding="utf-8") as f:
            f.write(creds.to_json())
    except OSError as e:
        logger.warning("Could not save token to %s: %s", TOKEN_PATH, e)


# ---------------------------------------------------------------------------
# Calendar lookup
# ---------------------------------------------------------------------------

def lookup_meeting(
    date_str: str,
    time_str: str,
    duration_minutes: int = 60,
) -> dict | None:
    """
    Find a Google Calendar event matching the recording's time window.

    Args:
        date_str: Recording date as "YYYY-MM-DD".
        time_str: Recording start time as "HH-MM".
        duration_minutes: Approximate meeting length (default 60).

    Returns:
        {"title": str, "participants": list[str], "description": str | None}
        or None if no matching event is found.
    """
    try:
        from googleapiclient.discovery import build
    except ImportError:
        logger.warning(
            "google-api-python-client not installed. Calendar lookup skipped."
        )
        return None

    creds = _get_credentials()
    if creds is None:
        return None

    # Parse the recording start time
    try:
        hour, minute = time_str.split("-")
        naive_start = datetime.strptime(f"{date_str} {hour}:{minute}", "%Y-%m-%d %H:%M")
    except ValueError as e:
        logger.warning("Could not parse date/time '%s %s': %s", date_str, time_str, e)
        return None

    # Build search window (±5 min buffer around start; extend to end of meeting)
    buffer = timedelta(minutes=5)
    window_start = naive_start - buffer
    window_end = naive_start + timedelta(minutes=duration_minutes) + buffer

    # Promote naive local times to aware datetimes for the Calendar API.
    #
    # Previous version stamped a fixed UTC-offset tzinfo (read from "now"
    # via astimezone().tzinfo) onto a recording timestamp from a different
    # date. That ignored DST: a recording from before a DST transition,
    # enriched after it, would be off by an hour and miss its event.
    # ZoneInfo carries the full DST history for the zone, so .replace(...)
    # produces a moment-correct aware datetime.
    local_tz = _local_zoneinfo()
    if local_tz is not None:
        aware_start = window_start.replace(tzinfo=local_tz)
        aware_end = window_end.replace(tzinfo=local_tz)
    else:
        aware_start = window_start.astimezone()
        aware_end = window_end.astimezone()
    time_min = aware_start.isoformat()
    time_max = aware_end.isoformat()

    logger.info(
        "Querying Google Calendar for events between %s and %s",
        time_min,
        time_max,
    )

    try:
        service = build("calendar", "v3", credentials=creds)

        # Determine the user's own email to filter from attendees.
        # googleapiclient raises HttpError + transport errors; broad catch
        # is intentional since missing own_email is harmless (attendee filter
        # just becomes a no-op).
        own_email: str | None = None
        try:
            cal_info = service.calendars().get(calendarId="primary").execute()
            own_email = cal_info.get("id")
        except Exception as e:  # noqa: BLE001 — degrade gracefully
            logger.warning("Could not determine user's email: %s", e)

        result = (
            service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception as e:  # noqa: BLE001 — Google API failures disable enrichment
        logger.error("Google Calendar API call failed: %s", e)
        return None

    events = result.get("items", [])
    if not events:
        logger.info("No calendar events found in the search window.")
        return None

    logger.info("Found %d candidate event(s) in search window.", len(events))

    # Pick the event whose start time is closest to the recording start
    naive_target = naive_start
    best_event = None
    best_delta = timedelta(days=999)

    for event in events:
        start_raw = event.get("start", {}).get("dateTime") or event.get("start", {}).get("date")
        if not start_raw:
            continue
        try:
            # dateTime includes timezone offset; date is all-day
            if "T" in start_raw:
                event_start = datetime.fromisoformat(start_raw).replace(tzinfo=None)
            else:
                event_start = datetime.strptime(start_raw, "%Y-%m-%d")
            delta = abs(event_start - naive_target)
            if delta < best_delta:
                best_delta = delta
                best_event = event
        except ValueError:
            continue

    if best_event is None:
        return None

    # Extract fields — log titles only at debug level (privacy)
    title: str = best_event.get("summary", "Untitled Meeting")
    logger.debug("Best matching event title: %r (delta=%s)", title, best_delta)

    # Build participant list
    raw_attendees = best_event.get("attendees", [])
    participants: list[str] = []
    for attendee in raw_attendees:
        email = attendee.get("email", "")
        # Skip the user's own account
        if own_email and email.lower() == own_email.lower():
            continue
        # Skip resource rooms / groups that declined
        if attendee.get("resource"):
            continue
        display = attendee.get("displayName")
        if display:
            participants.append(display)
        elif email:
            # Use the prefix before @ as a fallback
            participants.append(email.split("@")[0])

    # Meeting description / agenda (truncated)
    raw_desc: str | None = best_event.get("description")
    description: str | None = None
    if raw_desc:
        description = raw_desc.strip()[:500]

    return {
        "title": title,
        "participants": participants,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Pipeline helper
# ---------------------------------------------------------------------------

def enrich_metadata(wav_path: str) -> dict:
    """
    Parse source/date/time from wav_path, look up Google Calendar, and
    return a metadata dict suitable for merging into the pipeline's metadata.

    Returns:
        {"title": str, "participants": "Name1, Name2", "source": str}
        or {"source": str} if no calendar match is found.
    """
    basename = os.path.splitext(os.path.basename(wav_path))[0]
    match = re.match(
        r"^([a-zA-Z0-9_]+)_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2})$",
        basename,
    )
    if match:
        source = match.group(1)
        date_str = match.group(2)
        time_str = match.group(3)
    else:
        logger.warning(
            "enrich_metadata: could not parse filename '%s'. Skipping calendar lookup.",
            basename,
        )
        return {"source": "unknown"}

    result: dict = {"source": source}

    try:
        cal_match = lookup_meeting(date_str, time_str)
    except Exception as e:  # noqa: BLE001 — enrichment is non-fatal; any error is OK
        logger.warning("Calendar lookup raised an unexpected error: %s", e)
        cal_match = None

    if cal_match:
        result["title"] = cal_match["title"]
        if cal_match.get("participants"):
            result["participants"] = ", ".join(cal_match["participants"])
        if cal_match.get("description"):
            result["description"] = cal_match["description"]
        logger.info(
            "Calendar enrichment successful: %d participant(s) found.",
            len(cal_match.get("participants", [])),
        )
    else:
        logger.info("No calendar match found; returning source-only metadata.")

    return result
