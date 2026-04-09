"""
Pipeline — orchestrates the full transcription pipeline for MeetingNotes.

process_recording(wav_path) is the single entry point:
  1. Load sidecar metadata (.meta.json)
  2. Parse source / date / time from filename
  3. Build initial prompt from context.md (whisper mode)
  4. Transcribe with Parakeet (live) or whisper.cpp (batch)
  5. Summarize with Claude or Ollama
  6. Format and write the .md transcript
  7. Update state (transcripts_since_checkin, pending_deletion)
  8. Return the path to the written .md file

Can be run standalone:
    python -m app.pipeline Engine/recordings/queue/some-recording.wav
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sys
from datetime import datetime

from . import state as state_mod
from .calendar_lookup import enrich_metadata as _calendar_enrich
from .formatter import format_transcript, slugify
from .summarizer import summarize
from .transcriber import transcribe, transcribe_with_parakeet, build_initial_prompt

logger = logging.getLogger(__name__)

from .state import BASE_DIR, TRANSCRIPTS_DIR, CONTEXT_PATH, DONE_DIR


# --- Helpers -----------------------------------------------------------------

def _parse_filename(wav_path: str) -> tuple[str, str, str]:
    """
    Extract (source, date_str, time_str) from a filename like:
        zoom_2026-03-31_10-02.wav

    Returns empty strings for any parts that can't be parsed.
    """
    basename = os.path.splitext(os.path.basename(wav_path))[0]
    # Pattern: {source}_{YYYY-MM-DD}_{HH-MM}
    match = re.match(
        r"^([a-zA-Z0-9_]+)_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2})$",
        basename,
    )
    if match:
        return match.group(1), match.group(2), match.group(3)

    logger.warning(
        "Could not parse source/date/time from filename '%s'. "
        "Using defaults.",
        basename,
    )
    today = datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.now().strftime("%H-%M")
    return "unknown", today, now_time


def _load_sidecar(wav_path: str) -> dict:
    """
    Load optional .meta.json sidecar alongside the .wav file.

    e.g. zoom_2026-03-31_10-02.meta.json
    Returns an empty dict if the file doesn't exist or can't be parsed.
    """
    base = os.path.splitext(wav_path)[0]
    meta_path = base + ".meta.json"
    if not os.path.exists(meta_path):
        return {}
    try:
        with open(meta_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        logger.info("Loaded sidecar metadata from %s", meta_path)
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read sidecar %s: %s", meta_path, e)
        return {}


def _load_context_md() -> str:
    """Read Settings/context.md. Returns empty string on failure."""
    try:
        with open(CONTEXT_PATH, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        logger.warning("Could not read context.md: %s", e)
        return ""


def _write_transcript(content: str, date_str: str, title: str) -> str:
    """
    Write markdown content to transcripts/YYYY/MM/{date}_{slug}.md.

    Creates directories as needed. Returns the written file path.
    """
    try:
        year, month, _ = date_str.split("-")
    except ValueError:
        year = datetime.now().strftime("%Y")
        month = datetime.now().strftime("%m")

    out_dir = os.path.join(TRANSCRIPTS_DIR, year, month)
    os.makedirs(out_dir, exist_ok=True)

    slug = slugify(title)
    filename = f"{date_str}_{slug}.md"
    out_path = os.path.join(out_dir, filename)

    # Avoid clobbering an existing file with the same slug
    if os.path.exists(out_path):
        suffix = datetime.now().strftime("%H%M%S")
        filename = f"{date_str}_{slug}_{suffix}.md"
        out_path = os.path.join(out_dir, filename)

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("Transcript written to %s", out_path)
    return out_path


# --- Public API --------------------------------------------------------------

def process_recording(
    wav_path: str,
    pre_transcribed_text: str | None = None,
) -> str | None:
    """
    Run the full transcription pipeline for a .wav file.

    Args:
        wav_path: Path to a .wav file in recordings/queue/.
        pre_transcribed_text: If provided (from realtime transcription), skip
            the transcription step and use this text directly.

    Returns:
        Absolute path to the written .md transcript, or None on failure.
    """
    wav_path = os.path.expanduser(wav_path)
    logger.info("=== Pipeline start: %s ===", os.path.basename(wav_path))

    if not os.path.exists(wav_path):
        logger.error("WAV file not found: %s", wav_path)
        return None

    # Step 1: Load sidecar metadata
    metadata = _load_sidecar(wav_path)

    # Step 2: Parse filename for source, date, time
    source, date_str, time_str = _parse_filename(wav_path)
    # Metadata may override the filename-derived source
    if "source" not in metadata:
        metadata["source"] = source
    metadata["wav_filename"] = os.path.basename(wav_path)

    logger.info("source=%s  date=%s  time=%s", source, date_str, time_str)

    # Step 2b: Enrich metadata from Google Calendar (best-effort, non-fatal)
    try:
        cal_meta = _calendar_enrich(wav_path)
        # Calendar data fills gaps — sidecar values take priority
        for key, value in cal_meta.items():
            if key not in metadata:
                metadata[key] = value
            elif key == "participants" and not metadata.get("participants"):
                metadata[key] = value
        logger.info("Calendar enrichment applied to metadata.")
    except Exception as e:  # noqa: BLE001
        logger.warning("Calendar enrichment failed (non-fatal): %s", e)

    # Step 3 & 4: Transcribe (or use pre-transcribed text from realtime mode)
    transcription = None
    if pre_transcribed_text is not None:
        from .transcriber import TranscriptionResult
        if not pre_transcribed_text:
            logger.warning(
                "Pre-transcribed text is empty — realtime transcriber produced "
                "no output (recording may have been too short). Skipping pipeline."
            )
            return None
        logger.info("Using pre-transcribed text from realtime mode (%d chars)", len(pre_transcribed_text))
        transcription = TranscriptionResult(
            plain_text=pre_transcribed_text,
            timestamped_text=pre_transcribed_text,
            duration_minutes=0,
            srt_path="",
        )
    else:
        current_state = state_mod.load()
        mode = current_state.get("transcription_mode", "live")
        if mode not in ("live", "batch"):
            mode = "live"
        try:
            if mode == "live":
                logger.info("Transcribing with Parakeet (live mode, batch pass)")
                transcription = transcribe_with_parakeet(wav_path)
            else:
                initial_prompt = build_initial_prompt()
                logger.info("Transcribing with whisper.cpp (batch mode)")
                transcription = transcribe(wav_path, initial_prompt=initial_prompt or None)
            logger.info(
                "Transcription done (%s): %d chars, %d min",
                mode,
                len(transcription.plain_text),
                transcription.duration_minutes,
            )
        except Exception as e:  # noqa: BLE001
            logger.error("Transcription failed: %s", e)
            return None

    # Step 5: Load context.md for Claude
    context_md = _load_context_md()

    # Step 6: Summarize with Claude
    summary = None
    try:
        summary = summarize(
            transcript_text=transcription.timestamped_text,
            context_md=context_md,
            metadata=metadata,
        )
        logger.info("Summarization done: title=%r", summary.title)
    except Exception as e:  # noqa: BLE001
        logger.error(
            "Summarization failed (will save transcript without summary): %s", e
        )
        # Intentionally continue — a raw transcript is better than nothing

    # Step 7: Format the markdown content
    title = (summary.title if summary else None) or metadata.get(
        "title", f"{source.title()} Meeting"
    )

    try:
        md_content = format_transcript(
            transcription=transcription,
            summary=summary,
            metadata=metadata,
            source=source,
            date_str=date_str,
            time_str=time_str,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Formatter failed: %s", e)
        return None

    # Step 8: Write transcript to disk
    try:
        out_path = _write_transcript(md_content, date_str, title)
    except OSError as e:
        logger.error("Failed to write transcript file: %s", e)
        return None

    # Step 9: Update state and clean up recording
    try:
        current_state = state_mod.load()
        count = current_state.get("transcripts_since_checkin", 0) + 1
        retain = current_state.get("retain_recordings", False)

        state_updates: dict = {"transcripts_since_checkin": count}

        if retain:
            # Move .wav (and sidecars) to done/ for 14-day retention
            os.makedirs(DONE_DIR, exist_ok=True)
            done_wav = os.path.join(DONE_DIR, os.path.basename(wav_path))
            shutil.move(wav_path, done_wav)
            base = os.path.splitext(wav_path)[0]
            for ext in (".meta.json", ".srt"):
                src = base + ext
                if os.path.exists(src):
                    shutil.move(src, os.path.join(done_dir, os.path.basename(src)))
            pending = list(current_state.get("pending_deletion", []))
            # Deduplicate: don't add if this path is already pending
            if not any(e.get("path") == done_wav for e in pending):
                pending.append({"path": done_wav, "recorded_date": date_str})
            state_updates["pending_deletion"] = pending
            logger.info("Retained recording in %s", DONE_DIR)
        else:
            # Delete .wav and sidecars immediately
            base = os.path.splitext(wav_path)[0]
            for path in (wav_path, base + ".meta.json", base + ".srt"):
                if os.path.exists(path):
                    os.remove(path)
                    logger.info("Deleted: %s", path)

        state_mod.update(**state_updates)
        logger.info(
            "State updated: transcripts_since_checkin=%d, retain_recordings=%s",
            count,
            retain,
        )
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to update state (non-fatal): %s", e)

    logger.info("=== Pipeline complete: %s ===", out_path)
    return out_path


# --- CLI entry point ---------------------------------------------------------

def _configure_logging() -> None:
    """Set up basic console logging for standalone invocation."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )


if __name__ == "__main__":
    _configure_logging()

    if len(sys.argv) < 2:
        print("Usage: python -m app.pipeline <path/to/recording.wav>")
        sys.exit(1)

    wav = sys.argv[1]
    result = process_recording(wav)

    if result:
        print(f"\nTranscript saved to:\n  {result}")
        sys.exit(0)
    else:
        print("\nPipeline failed — check logs above for details.")
        sys.exit(1)
