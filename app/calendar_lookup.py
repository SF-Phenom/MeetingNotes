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
from datetime import datetime, timedelta, timezone

from app.state import BASE_DIR

logger = logging.getLogger(__name__)
CREDENTIALS_DIR = os.path.join(BASE_DIR, ".credentials")
CLIENT_SECRET_PATH = os.path.join(CREDENTIALS_DIR, "google_oauth_client.json")
TOKEN_PATH = os.path.join(CREDENTIALS_DIR, "google_token.json")

SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def _get_credentials():
    """
    Load saved credentials or run the OAuth consent flow.

    Returns a valid Credentials object, or None if authentication fails.
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

    # Load saved token if it exists
    if os.path.exists(TOKEN_PATH):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not load saved token (%s); will re-authenticate.", e)
            creds = None

    # Refresh or re-authenticate as needed
    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            _save_token(creds)
            return creds
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Token refresh failed (%s); deleting stale token and re-authenticating.", e
            )
            try:
                os.remove(TOKEN_PATH)
            except OSError:
                pass
            creds = None

    # First-time or re-auth flow
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
    except Exception as e:  # noqa: BLE001
        logger.error("OAuth flow failed: %s", e)
        return None


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
        from googleapiclient.errors import HttpError
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

    # Use UTC — Calendar API requires RFC 3339 with timezone
    # Assume local system timezone for conversion
    local_tz = datetime.now(timezone.utc).astimezone().tzinfo
    time_min = window_start.replace(tzinfo=local_tz).isoformat()
    time_max = window_end.replace(tzinfo=local_tz).isoformat()

    logger.info(
        "Querying Google Calendar for events between %s and %s",
        time_min,
        time_max,
    )

    try:
        service = build("calendar", "v3", credentials=creds)

        # Determine the user's own email to filter from attendees
        own_email: str | None = None
        try:
            cal_info = service.calendars().get(calendarId="primary").execute()
            own_email = cal_info.get("id")  # primary calendar id is the user's email
        except Exception as e:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001
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
    except Exception as e:  # noqa: BLE001
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
