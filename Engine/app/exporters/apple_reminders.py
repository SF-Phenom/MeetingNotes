"""Apple Reminders backend for the action-item exporter.

Talks to Reminders.app via ``osascript``. We build one AppleScript per
batch (cheaper than osascript-per-item — each invocation is ~150ms cold)
and stream it via stdin so item text never lands on a command line.

AppleScript string interpolation is the obvious injection vector: items
come from the LLM summarizer, which in turn reads transcript text the
user doesn't fully control. ``_escape`` is conservative — backslash
first, then quotes, then newlines/CR — so a crafted item can't break
out of its quoted literal.
"""
from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


# Generous because launching osascript + Reminders cold can take a few
# seconds the first time after boot. We don't want to give up so fast
# that a slow first call eats the user's items.
_OSASCRIPT_TIMEOUT = 30.0


def _escape(s: str) -> str:
    """Escape a string for safe interpolation inside AppleScript double quotes.

    Order matters: backslash first (otherwise the escapes we add below
    get re-escaped). Newlines become ``\\n`` because AppleScript treats
    that as a literal newline inside a double-quoted string. Carriage
    returns are dropped — they'd reach Reminders and look like garbage.
    """
    return (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "")
    )


def _format_body(item: dict, source_label: str) -> str:
    """Build the note body shown under each reminder.

    Lists owner / due if the summarizer pulled them out, plus a "From: …"
    line so the user can find the meeting later. Empty if there's nothing
    to add — Reminders displays the title-only.
    """
    parts: list[str] = []
    owner = item.get("owner")
    if owner:
        parts.append("Owner: {}".format(owner))
    due = item.get("due")
    if due:
        parts.append("Due: {}".format(due))
    if source_label:
        parts.append("From: {}".format(source_label))
    return "\n".join(parts)


def _build_script(items: list[dict], list_name: str, source_label: str) -> str:
    """Assemble the full AppleScript for a batch."""
    list_esc = _escape(list_name)
    lines: list[str] = [
        'tell application "Reminders"',
        '    if not (exists list "{}") then'.format(list_esc),
        '        make new list with properties {{name:"{}"}}'.format(list_esc),
        '    end if',
        '    tell list "{}"'.format(list_esc),
    ]
    for item in items:
        name = _escape(str(item.get("item", "")).strip())
        if not name:
            continue  # Reminders refuses an empty name.
        body = _escape(_format_body(item, source_label))
        if body:
            lines.append(
                '        make new reminder with properties '
                '{{name:"{}", body:"{}"}}'.format(name, body)
            )
        else:
            lines.append(
                '        make new reminder with properties '
                '{{name:"{}"}}'.format(name)
            )
    lines.append('    end tell')
    lines.append('end tell')
    return "\n".join(lines)


def add_items(
    items: list[dict],
    list_name: str = "MeetingNotes",
    source_label: str = "",
) -> tuple[int, list[str]]:
    """Add ``items`` to the named Reminders list.

    Returns ``(count_successfully_added, errors)``. The osascript call is
    all-or-nothing per batch: if it fails, count is 0 and the stderr is
    surfaced as a single error string. Items with empty names are
    silently skipped before the script is built.
    """
    queued = [it for it in items if str(it.get("item", "")).strip()]
    if not queued:
        return 0, []

    script = _build_script(queued, list_name=list_name, source_label=source_label)

    try:
        result = subprocess.run(
            ["osascript", "-"],
            input=script,
            text=True,
            capture_output=True,
            timeout=_OSASCRIPT_TIMEOUT,
        )
    except FileNotFoundError:
        return 0, ["osascript not found — Apple Reminders export only works on macOS."]
    except subprocess.TimeoutExpired:
        return 0, [
            "osascript timed out after {:.0f}s — is Reminders.app responding?".format(
                _OSASCRIPT_TIMEOUT,
            )
        ]

    if result.returncode != 0:
        err = result.stderr.strip() or "osascript exited {}".format(result.returncode)
        logger.warning("Apple Reminders export failed: %s", err)
        return 0, [err]

    return len(queued), []
