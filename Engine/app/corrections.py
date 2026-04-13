"""
Corrections — post-transcription find-and-replace from Settings/corrections.md.

Parses a user-editable markdown file with two sections:
  - ## Terms: vocabulary words (for future engine biasing)
  - ## Replacements: a markdown table of heard → correct pairs

Corrections are applied case-insensitively with word-boundary matching.
The file is re-read automatically when it changes on disk.
"""

from __future__ import annotations

import logging
import os
import re

from app.state import BASE_DIR

logger = logging.getLogger(__name__)

CORRECTIONS_PATH = os.path.join(BASE_DIR, "Settings", "corrections.md")

# Module-level cache
_cached_mtime: float = 0
_cached_terms: list[str] = []
_cached_replacements: list[tuple[re.Pattern, str]] = []


def _parse_corrections_md(path: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Parse corrections.md into (terms, [(heard, should_be), ...])."""
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        logger.debug("corrections.md not found at %s — no corrections loaded", path)
        return [], []
    except OSError as e:
        logger.warning("Could not read corrections.md: %s", e)
        return [], []

    terms: list[str] = []
    replacements: list[tuple[str, str]] = []

    section = None
    in_table = False

    for line in text.splitlines():
        stripped = line.strip()

        # Detect section headers
        if stripped.lower().startswith("## terms"):
            section = "terms"
            in_table = False
            continue
        elif stripped.lower().startswith("## replacements"):
            section = "replacements"
            in_table = False
            continue
        elif stripped.startswith("## "):
            section = None
            in_table = False
            continue

        if section == "terms":
            # Parse bullet items: "- Term"
            m = re.match(r"^[-*]\s+(.+)$", stripped)
            if m:
                term = m.group(1).strip()
                if term:
                    terms.append(term)

        elif section == "replacements":
            # Skip the header row and separator row of the markdown table
            if stripped.startswith("|") and "---" in stripped:
                in_table = True
                continue
            if stripped.lower().startswith("| heard"):
                in_table = False
                continue

            # Parse table rows: "| heard | should be |"
            if stripped.startswith("|"):
                in_table = True
                cells = [c.strip() for c in stripped.split("|")]
                # split on | gives ['', 'heard', 'should be', '']
                cells = [c for c in cells if c]
                if len(cells) >= 2:
                    heard, should_be = cells[0], cells[1]
                    if heard and should_be:
                        replacements.append((heard, should_be))

    return terms, replacements


def _compile_replacements(
    raw: list[tuple[str, str]],
) -> list[tuple[re.Pattern, str]]:
    """Compile (heard, should_be) pairs into regex patterns with word boundaries."""
    compiled = []
    for heard, should_be in raw:
        try:
            pattern = re.compile(
                r"\b" + re.escape(heard) + r"\b",
                re.IGNORECASE,
            )
            compiled.append((pattern, should_be))
        except re.error as e:
            logger.warning("Invalid correction pattern %r: %s", heard, e)
    return compiled


def _load() -> None:
    """Reload corrections.md if it has changed on disk."""
    global _cached_mtime, _cached_terms, _cached_replacements

    try:
        mtime = os.path.getmtime(CORRECTIONS_PATH)
    except OSError:
        # File doesn't exist or can't be stat'd — clear cache
        _cached_mtime = 0
        _cached_terms = []
        _cached_replacements = []
        return

    if mtime == _cached_mtime:
        return

    terms, raw_replacements = _parse_corrections_md(CORRECTIONS_PATH)
    compiled = _compile_replacements(raw_replacements)

    _cached_mtime = mtime
    _cached_terms = terms
    _cached_replacements = compiled

    logger.info(
        "Loaded corrections.md: %d terms, %d replacements",
        len(terms),
        len(compiled),
    )


def get_terms() -> list[str]:
    """Return the list of vocabulary terms from corrections.md."""
    _load()
    return list(_cached_terms)


def apply_corrections(text: str) -> str:
    """Apply all replacement rules to the given text. Returns corrected text."""
    _load()
    if not _cached_replacements:
        return text
    for pattern, replacement in _cached_replacements:
        text = pattern.sub(replacement, text)
    return text
