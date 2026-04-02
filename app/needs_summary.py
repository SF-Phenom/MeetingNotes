"""
Scans transcripts and identifies which ones need summarization or re-summarization.

A transcript "needs summary" if:
  - It contains "Summary unavailable" (no summary was generated)
  - Its frontmatter has model: ollama:* (was done by a local model, could be improved)

Usage:
    python -m app.needs_summary
"""

from __future__ import annotations

import os
import sys

from app.state import BASE_DIR
TRANSCRIPTS_DIR = os.path.join(BASE_DIR, "transcripts")


def scan_transcripts() -> dict:
    """Return categorized lists of transcript paths needing attention."""
    no_summary: list[str] = []
    local_model: list[str] = []
    complete: list[str] = []

    if not os.path.isdir(TRANSCRIPTS_DIR):
        return {"no_summary": no_summary, "local_model": local_model, "complete": complete}

    for dirpath, _dirs, files in os.walk(TRANSCRIPTS_DIR):
        for fname in sorted(files):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(dirpath, fname)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue

            rel = os.path.relpath(fpath, BASE_DIR)

            if "Summary unavailable" in content or "_Summary unavailable" in content:
                no_summary.append(rel)
            elif "model: ollama:" in content:
                local_model.append(rel)
            else:
                complete.append(rel)

    return {"no_summary": no_summary, "local_model": local_model, "complete": complete}


if __name__ == "__main__":
    results = scan_transcripts()

    if results["no_summary"]:
        print("=== NEEDS SUMMARY (no summary generated) ===")
        for path in results["no_summary"]:
            print(f"  {path}")
        print()

    if results["local_model"]:
        print("=== LOCAL MODEL (could be improved with Claude) ===")
        for path in results["local_model"]:
            print(f"  {path}")
        print()

    if results["complete"]:
        print("=== COMPLETE (Claude-generated summary) ===")
        for path in results["complete"]:
            print(f"  {path}")
        print()

    total_needing = len(results["no_summary"]) + len(results["local_model"])
    if total_needing == 0:
        print("All transcripts are up to date.")
    else:
        print(f"{total_needing} transcript(s) could use summarization.")
