"""
Check-in system for MeetingNotes.

Determines when the user should do a CoWork (Claude Code) check-in session
to update their project knowledge base, generates the prompt, and marks
check-ins complete.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import date, datetime

from app import state

logger = logging.getLogger(__name__)

BASE_DIR = os.path.expanduser("~/MeetingNotes")
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")
PROJECTS_DIR = os.path.join(BASE_DIR, "projects")
CONTEXT_PATH = os.path.join(BASE_DIR, "context.md")

CHECKIN_TRANSCRIPT_THRESHOLD = 6
CHECKIN_DAY_THRESHOLD = 14


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def should_trigger_checkin() -> bool:
    """Return True if the user should be prompted to do a check-in session.

    Triggers when:
    - transcripts_since_checkin >= 6, OR
    - days since last_checkin_date >= 14 AND at least 1 transcript exists
      since the last check-in.

    If last_checkin_date is None (never checked in), the day threshold is
    skipped and only the transcript count matters.
    """
    s = state.load()
    count = s.get("transcripts_since_checkin", 0)
    last_checkin = s.get("last_checkin_date")  # str "YYYY-MM-DD" or None

    if count >= CHECKIN_TRANSCRIPT_THRESHOLD:
        logger.debug("Check-in triggered: %d transcripts since last check-in", count)
        return True

    if last_checkin is not None and count >= 1:
        try:
            last_date = date.fromisoformat(last_checkin)
            days_elapsed = (date.today() - last_date).days
            if days_elapsed >= CHECKIN_DAY_THRESHOLD:
                logger.debug(
                    "Check-in triggered: %d days since last check-in", days_elapsed
                )
                return True
        except ValueError:
            logger.warning("Could not parse last_checkin_date: %r", last_checkin)

    return False


def generate_checkin_prompt() -> str:
    """Build and return the full check-in prompt string."""
    s = state.load()
    last_checkin = s.get("last_checkin_date")  # str or None

    # --- context.md ---
    context_text = _read_context()

    # --- transcripts ---
    transcripts = _list_new_transcripts(last_checkin)
    if transcripts:
        transcript_lines = "\n".join(
            f"- {t['date']} — {t['title']} ({_rel(t['path'])})"
            for t in transcripts
        )
    else:
        transcript_lines = "- (no new transcripts found)"

    # --- project files ---
    projects = _list_project_files()
    if projects:
        project_lines = "\n".join(
            f"- {p['title']} ({_rel(p['path'])})" for p in projects
        )
    else:
        project_lines = "No project files yet."

    since_label = last_checkin if last_checkin else "never"

    prompt = (
        "Please help me update my project knowledge base based on recent meetings.\n"
        "\n"
        "Context about my role and team:\n"
        f"{context_text}\n"
        "\n"
        f"New transcripts since last check-in ({since_label}):\n"
        f"{transcript_lines}\n"
        "\n"
        "Current project files:\n"
        f"{project_lines}\n"
        "\n"
        "Please:\n"
        "1. Read each of the transcript files listed above\n"
        "2. Identify which existing projects each new meeting relates to\n"
        "3. Identify any new projects or initiatives that appear to have emerged\n"
        "4. Note my specific contributions and any accomplishments worth logging\n"
        "5. Flag anything you're uncertain about and ask me to clarify\n"
        "6. Summarize proposed updates by project before making any changes\n"
        "\n"
        "After I confirm, update the project .md files accordingly."
    )
    return prompt


def copy_prompt_to_clipboard() -> bool:
    """Generate the check-in prompt and copy it to the macOS clipboard.

    Returns True on success, False on failure.
    """
    try:
        prompt = generate_checkin_prompt()
        result = subprocess.run(
            ["pbcopy"],
            input=prompt.encode(),
            check=True,
        )
        logger.info("Check-in prompt copied to clipboard (%d chars)", len(prompt))
        return True
    except subprocess.CalledProcessError as e:
        logger.error("pbcopy failed: %s", e)
        return False
    except Exception as e:
        logger.error("Unexpected error copying prompt to clipboard: %s", e)
        return False


def mark_checkin_complete() -> None:
    """Reset the transcript counter and record today as the last check-in date."""
    today = date.today().isoformat()
    state.update(transcripts_since_checkin=0, last_checkin_date=today)
    logger.info("Check-in marked complete. last_checkin_date=%s", today)


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _rel(path: str) -> str:
    """Return path relative to BASE_DIR (~/MeetingNotes/)."""
    try:
        return os.path.relpath(path, BASE_DIR)
    except ValueError:
        return path


def _read_context() -> str:
    """Read context.md, returning its contents or a placeholder on failure."""
    try:
        with open(CONTEXT_PATH, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError as e:
        logger.warning("Could not read context.md: %s", e)
        return "(context.md not found)"


def _parse_frontmatter(lines: list[str]) -> dict[str, str]:
    """Extract key-value pairs from YAML frontmatter delimited by '---' lines.

    Parses lines between the first and second '---' markers.
    Only handles simple scalar values (no lists/nested keys).
    """
    fm: dict[str, str] = {}
    if not lines or lines[0].strip() != "---":
        return fm
    in_fm = False
    for line in lines:
        stripped = line.strip()
        if stripped == "---":
            if not in_fm:
                in_fm = True
                continue
            else:
                break  # end of frontmatter
        if in_fm and ": " in stripped:
            key, _, value = stripped.partition(": ")
            fm[key.strip()] = value.strip()
    return fm


def _extract_title(lines: list[str]) -> str:
    """Return the text of the first '# Heading' line, or 'Untitled'."""
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return "Untitled"


def _list_new_transcripts(since_date: str | None) -> list[dict]:
    """Return transcript metadata for .md files newer than since_date.

    Each entry: {"path": str, "date": str, "title": str}
    Sorted ascending by date.
    """
    results: list[dict] = []

    if not os.path.isdir(TRANSCRIPTS_DIR):
        logger.debug("Transcripts directory not found: %s", TRANSCRIPTS_DIR)
        return results

    for dirpath, _dirnames, filenames in os.walk(TRANSCRIPTS_DIR):
        for fname in filenames:
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except OSError as e:
                logger.warning("Could not read transcript %s: %s", fpath, e)
                continue

            fm = _parse_frontmatter(lines)
            # Prefer frontmatter date; fall back to file mtime
            file_date = fm.get("date", "")
            if not file_date:
                mtime = os.path.getmtime(fpath)
                file_date = datetime.fromtimestamp(mtime).date().isoformat()

            if since_date and file_date <= since_date:
                continue

            title = _extract_title(lines)
            results.append({"path": fpath, "date": file_date, "title": title})

    results.sort(key=lambda t: t["date"])
    return results


def _list_project_files() -> list[dict]:
    """Return metadata for all .md files in the projects directory.

    Each entry: {"path": str, "title": str}
    """
    results: list[dict] = []

    if not os.path.isdir(PROJECTS_DIR):
        logger.debug("Projects directory not found: %s", PROJECTS_DIR)
        return results

    for fname in sorted(os.listdir(PROJECTS_DIR)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(PROJECTS_DIR, fname)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except OSError as e:
            logger.warning("Could not read project file %s: %s", fpath, e)
            continue

        title = _extract_title(lines)
        results.append({"path": fpath, "title": title})

    return results
