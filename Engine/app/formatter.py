"""
Formatter — builds the .md transcript file for MeetingNotes.

Takes structured transcription and summary data and returns a markdown string
suitable for writing to transcripts/YYYY/MM/.
"""

from __future__ import annotations

import re
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


# --- Helpers -----------------------------------------------------------------

def slugify(title: str) -> str:
    """
    Convert a meeting title to a URL/filename-safe slug.

    "Weekly PMM Sync" -> "weekly-pmm-sync"
    """
    slug = title.lower()
    slug = re.sub(r"[^\w\s-]", "", slug)       # remove punctuation
    slug = re.sub(r"[\s_]+", "-", slug)         # spaces/underscores -> hyphen
    slug = re.sub(r"-{2,}", "-", slug)           # collapse multiple hyphens
    slug = slug.strip("-")
    return slug or "untitled"


def _format_time_display(time_str: str) -> str:
    """
    Convert "10-02" (HH-MM) to "10:02 AM" / "2:34 PM".
    Handles both "HH-MM" and "HH:MM" input formats.
    """
    time_str = time_str.replace("-", ":")
    try:
        dt = datetime.strptime(time_str, "%H:%M")
        return dt.strftime("%-I:%M %p")
    except ValueError:
        return time_str


def _format_source_display(source: str) -> str:
    """Capitalize source name nicely: 'zoom' -> 'Zoom', 'teams' -> 'Teams'."""
    known = {
        "zoom": "Zoom",
        "teams": "Teams",
        "meet": "Google Meet",
        "google-meet": "Google Meet",
        "google_meet": "Google Meet",
        "slack": "Slack Huddle",
        "webex": "Webex",
        "facetime": "FaceTime",
        "manual": "Manual Recording",
        "phone": "Phone",
    }
    return known.get(source.lower(), source.title())


# --- Public API --------------------------------------------------------------

def format_transcript(
    transcription,       # TranscriptionResult (typed loosely to avoid circular import)
    summary,             # SummaryResult | None
    metadata: dict,
    source: str,
    date_str: str,       # "2026-03-31"
    time_str: str,       # "10-02"
) -> str:
    """
    Build the full markdown content for a transcript file.

    Args:
        transcription: TranscriptionResult from transcriber.py
        summary: SummaryResult from summarizer.py, or None if summarization failed
        metadata: Dict with optional keys: participants, notes, source
        source: Recording source (e.g. "zoom", "teams")
        date_str: ISO date string "YYYY-MM-DD"
        time_str: Time string "HH-MM" or "HH:MM"

    Returns:
        Complete markdown string.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    time_display = _format_time_display(time_str)
    source_display = _format_source_display(source)
    duration_min = getattr(transcription, "duration_minutes", 0)
    wav_basename = metadata.get("wav_filename", "")

    # Title priority: metadata['title'] is authoritative (pipeline pre-
    # resolves the calendar > LLM > source-fallback order and any Part N
    # suffix before calling us). Formatter-local fallbacks exist only so
    # direct callers (tests, ad-hoc tooling) still get a sensible default.
    title = metadata.get("title")
    if not title:
        title = (summary.title if summary else None) or f"{source_display} Meeting"

    # Frontmatter participants — prefer metadata, fall back to unknown
    participants_display = metadata.get("participants", "unknown")

    lines: list[str] = []

    # --- YAML Frontmatter ---
    lines.append("---")
    lines.append(f"date: {date_str}")
    lines.append(f"time: {time_display}")
    lines.append(f"duration: {duration_min} min")
    lines.append(f"source: {source_display}")
    lines.append(f"participants: {participants_display}")
    if wav_basename:
        lines.append(f"recording: {wav_basename}")
    # Calendar event ID — enables the Part N collision detector in
    # pipeline._find_prior_parts to stitch repeat recordings of the same
    # meeting together without relying on fragile filename matching.
    event_id = metadata.get("calendar_event_id")
    if event_id:
        lines.append(f"calendar_event_id: {event_id}")
    lines.append(f"transcribed: {today}")
    model_used = getattr(summary, "model_used", "") if summary else ""
    if model_used:
        lines.append(f"model: {model_used}")
    lines.append("---")
    lines.append("")

    # --- Title ---
    lines.append(f"# {title}")
    lines.append("")

    if summary:
        # --- Summary section ---
        lines.append("## Summary")
        lines.append(summary.summary or "_No summary available._")
        lines.append("")

        # --- Action Items ---
        lines.append("## Action Items")
        if summary.action_items:
            for item in summary.action_items:
                text = item.get("item", "")
                owner = item.get("owner")
                due = item.get("due")
                parts = [f"- [ ] {text}"]
                if owner:
                    parts.append(f"— {owner}")
                if due:
                    parts.append(f"by {due}")
                lines.append(" ".join(parts))
        else:
            lines.append("_No action items identified._")
        lines.append("")

        # --- Key Decisions ---
        lines.append("## Key Decisions")
        if summary.key_decisions:
            for decision in summary.key_decisions:
                lines.append(f"- {decision}")
        else:
            lines.append("_No key decisions recorded._")
        lines.append("")

        # --- Projects Mentioned ---
        if summary.projects_mentioned:
            lines.append("## Projects Mentioned")
            lines.append(", ".join(summary.projects_mentioned))
            lines.append("")

    else:
        # Graceful degradation — Claude failed
        lines.append("## Summary")
        lines.append("_Summary unavailable — Summarization failed during processing._")
        lines.append("")
        lines.append("## Action Items")
        lines.append("_Action items unavailable._")
        lines.append("")
        lines.append("## Key Decisions")
        lines.append("_Key decisions unavailable._")
        lines.append("")

    # --- Full Transcript ---
    lines.append("## Full Transcript")
    lines.append("")
    timestamped = getattr(transcription, "timestamped_text", "")
    if timestamped.strip():
        lines.append(timestamped.strip())
    else:
        plain = getattr(transcription, "plain_text", "")
        lines.append(plain.strip() if plain.strip() else "_Transcript unavailable._")
    lines.append("")

    return "\n".join(lines)
