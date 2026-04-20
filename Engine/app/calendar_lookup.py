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

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
# Historical scope used by pre-Tweak-B tokens. When we detect a token with
# only the narrower scope, _get_credentials refuses to use it — forces re-auth
# so the wider scope (needed for primary calendar metadata → user email →
# attendee filter) gets granted.
_LEGACY_SCOPES = ["https://www.googleapis.com/auth/calendar.events.readonly"]


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

        # Detect a token granted under the legacy narrow scope. Those tokens
        # still authenticate but can't hit calendars.get(primary), so the
        # attendee-filter path silently degrades. Force re-auth.
        if creds is not None and not _scopes_satisfy(creds):
            logger.info("Saved token lacks current scope set; re-auth required.")
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


def _scopes_satisfy(creds) -> bool:
    """Return True if the granted credentials cover every scope we require.

    google-auth stores granted scopes on ``creds.scopes`` (list or None).
    We require every scope in SCOPES to be present; extras are fine.
    """
    granted = set(getattr(creds, "scopes", None) or [])
    return all(scope in granted for scope in SCOPES)


def reset_auth() -> bool:
    """Delete the saved Google token so the next authorize_interactive() call
    forces a fresh consent flow. Returns True if a token was removed, False
    if there was nothing to remove.
    """
    if not os.path.exists(TOKEN_PATH):
        return False
    try:
        os.remove(TOKEN_PATH)
        logger.info("Google Calendar token cleared; re-auth required.")
        return True
    except OSError as e:
        logger.warning("Could not remove token at %s: %s", TOKEN_PATH, e)
        return False


def test_connection() -> dict:
    """Probe the current Google Calendar auth + scope + event-fetch path.

    Used by the menubar "Test Calendar Connection" action and the
    setup.command validity probe. Returns a dict describing what worked —
    caller formats it for display.

    Keys:
        status: one of "ok", "not_authorized", "scope_outdated", "error"
        detail: short human-readable summary
        email: str | None — primary calendar ID (== user email) when scope allows
        upcoming_count: int — number of events fetched in the probe window
    """
    result: dict = {
        "status": "error",
        "detail": "",
        "email": None,
        "upcoming_count": 0,
    }

    if not os.path.exists(TOKEN_PATH):
        result["status"] = "not_authorized"
        result["detail"] = "No saved Google Calendar token. Sign in from the menubar."
        return result

    try:
        from googleapiclient.discovery import build
    except ImportError:
        result["detail"] = (
            "google-api-python-client not installed. Run setup.command to restore the venv."
        )
        return result

    creds = _get_credentials()
    if creds is None:
        # _get_credentials returns None for both missing and scope-outdated
        # tokens. Distinguish so the UI can say the right thing.
        try:
            from google.oauth2.credentials import Credentials
            raw = Credentials.from_authorized_user_file(TOKEN_PATH, _LEGACY_SCOPES)
        except Exception:  # noqa: BLE001 — any failure = token is dead
            raw = None
        if raw is not None and not _scopes_satisfy(raw):
            result["status"] = "scope_outdated"
            result["detail"] = (
                "Saved token is missing the calendar.readonly scope. "
                "Choose Re-authenticate to upgrade."
            )
        else:
            result["status"] = "not_authorized"
            result["detail"] = (
                "Token is invalid or expired beyond refresh. Sign in again."
            )
        return result

    try:
        service = build("calendar", "v3", credentials=creds)
        cal_info = service.calendars().get(calendarId="primary").execute()
        result["email"] = cal_info.get("id")

        # Look a few hours ahead for upcoming events — confirms events.list
        # works end-to-end, not just the metadata call.
        now = datetime.utcnow()
        time_min = now.isoformat() + "Z"
        time_max = (now + timedelta(hours=24)).isoformat() + "Z"
        events = service.events().list(
            calendarId="primary",
            timeMin=time_min,
            timeMax=time_max,
            maxResults=10,
            singleEvents=True,
            orderBy="startTime",
        ).execute()
        result["upcoming_count"] = len(events.get("items", []))
        result["status"] = "ok"
        result["detail"] = (
            f"Connected as {result['email']}. "
            f"{result['upcoming_count']} event(s) in the next 24 hours."
        )
    except Exception as e:  # noqa: BLE001 — any Google API failure is a probe failure
        result["status"] = "error"
        result["detail"] = f"Google Calendar probe failed: {e}"

    return result


# ---------------------------------------------------------------------------
# Calendar lookup
# ---------------------------------------------------------------------------

# Pre-event association buffer. A recording started up to this many minutes
# before an event's scheduled start is still associated with that event —
# gives the user leeway to arm the recorder early.
ASSOC_PRE_BUFFER = timedelta(minutes=10)


def _parse_event_time(event: dict, key: str) -> datetime | None:
    """Parse an event's start/end dateTime as a naive local datetime.

    All-day events (which use 'date' not 'dateTime') return None — we
    deliberately skip those for association because blocks like "PTO" or
    "Focus Time" shouldn't title a recording.
    """
    raw = event.get(key, {}).get("dateTime")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw).replace(tzinfo=None)
    except ValueError:
        return None


def _select_event(events: list[dict], recording_start: datetime) -> dict | None:
    """Apply the association rule + tiebreaker to a list of candidate events.

    Rule: recording_start must satisfy
        event.start - 10 min  <=  recording_start  <=  event.end

    Tiebreaker (back-to-back meetings): prefer events whose start is
    >= recording_start (upcoming). Among those, pick the earliest start —
    that's the meeting the user is about to have, not the one just ending.
    If every qualifying event has already started, fall back to the one
    whose start is closest to recording_start.
    """
    candidates: list[tuple[dict, datetime, datetime]] = []
    for event in events:
        event_start = _parse_event_time(event, "start")
        event_end = _parse_event_time(event, "end")
        if event_start is None or event_end is None:
            continue
        if event_start - ASSOC_PRE_BUFFER <= recording_start <= event_end:
            candidates.append((event, event_start, event_end))

    if not candidates:
        return None

    upcoming = [c for c in candidates if c[1] >= recording_start]
    if upcoming:
        chosen = min(upcoming, key=lambda c: c[1])
    else:
        chosen = min(candidates, key=lambda c: abs(c[1] - recording_start))
    return chosen[0]


def lookup_meeting(date_str: str, time_str: str) -> dict | None:
    """
    Find a Google Calendar event associated with the recording's start time.

    Association rule: the event is associated iff
        event.start - 10 min  <=  recording_start  <=  event.end

    Args:
        date_str: Recording date as "YYYY-MM-DD".
        time_str: Recording start time as "HH-MM".

    Returns:
        {"title": str, "participants": list[str], "description": str | None,
         "event_id": str} or None if no associated event is found.
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

    # Build Google-API query window. We need to fetch any event that *could*
    # satisfy the association rule so _select_event can apply it precisely.
    # Google's events.list filter returns events where
    #     event.end > timeMin  AND  event.start < timeMax
    # so:
    #   timeMin = recording_start - 1 min  → catches events still in progress
    #             and ones ending at the recording moment.
    #   timeMax = recording_start + 11 min → catches events starting up to
    #             10 min after (the pre-buffer), plus 1 min of slack.
    # Client-side, _select_event enforces the strict rule.
    query_start = naive_start - timedelta(minutes=1)
    query_end = naive_start + timedelta(minutes=11)

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
        aware_start = query_start.replace(tzinfo=local_tz)
        aware_end = query_end.replace(tzinfo=local_tz)
    else:
        aware_start = query_start.astimezone()
        aware_end = query_end.astimezone()
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
        logger.info("No calendar events found in the association window.")
        return None

    logger.info("Fetched %d candidate event(s); applying association rule.", len(events))

    best_event = _select_event(events, naive_start)
    if best_event is None:
        logger.info("No event satisfies the association rule — returning no match.")
        return None

    # Extract fields — log titles only at debug level (privacy)
    title: str = best_event.get("summary", "Untitled Meeting")
    logger.debug("Associated event title: %r", title)

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
        "event_id": best_event.get("id", ""),
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
        if cal_match.get("event_id"):
            result["calendar_event_id"] = cal_match["event_id"]
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
