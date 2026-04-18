"""
Pipeline — orchestrates the full transcription pipeline for MeetingNotes.

process_recording(wav_path) is the single entry point:
  1. Load sidecar metadata (.meta.json)
  2. Parse source / date / time from filename
  3. Transcribe with Parakeet
  4. Summarize with Claude or Ollama
  5. Format and write the .md transcript
  6. Update state (transcripts_since_checkin, pending_deletion)
  7. Return the path to the written .md file

Can be run standalone:
    python -m app.pipeline Engine/recordings/queue/some-recording.wav
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime
from typing import Callable

from . import state as state_mod
from .calendar_lookup import enrich_metadata as _calendar_enrich
from .exporter import ExportResult, export_action_items
from .formatter import format_transcript, slugify
from .recording_file import RecordingFile
from .summarizer import summarize
from .transcription_engine import get_batch_engine

logger = logging.getLogger(__name__)

from .environment import BASE_DIR, TRANSCRIPTS_DIR, CONTEXT_PATH, DONE_DIR


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

    # Atomic write: a crash mid-write would otherwise leave the user with
    # an empty or half-written transcript at a name they'll trust on
    # reopen. Write to a sibling .tmp, fsync, then rename — POSIX rename
    # is atomic on the same filesystem.
    tmp_path = out_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp_path, out_path)

    logger.info("Transcript written to %s", out_path)
    return out_path


# --- Public API --------------------------------------------------------------

def _remove_if_exists(path: str) -> None:
    """Best-effort unlink — the caller's OK with the file not being there."""
    try:
        os.remove(path)
        logger.info("Removed: %s", path)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not remove %s: %s", path, e)


def process_recording(
    wav_path: str,
    pre_transcribed_text: str | None = None,
    *,
    on_summary_fallback: Callable[[], None] | None = None,
    on_export: Callable[[ExportResult], None] | None = None,
    on_too_short: Callable[[], None] | None = None,
) -> str | None:
    """
    Run the full transcription pipeline for a .wav file.

    Args:
        wav_path: Path to a .wav file in recordings/queue/.
        pre_transcribed_text: If provided (from realtime transcription), skip
            the transcription step and use this text directly.
        on_summary_fallback: Optional zero-arg callback fired when the
            summarizer was in automatic mode and fell back from Claude to
            Ollama. Used by the menubar to surface the degradation —
            otherwise the user has no idea their summary came from a
            local model.
        on_export: Optional callback receiving the ExportResult from
            step 8.5 — only fires when an exporter backend is configured
            (i.e. result.attempted is True). Used by the menubar to
            notify "N items added to Reminders" or surface errors.
        on_too_short: Optional zero-arg callback fired when the recording
            was shorter than Parakeet's 16-sec realtime chunk threshold
            (so pre_transcribed_text came in empty). The pipeline deletes
            the WAV + .live.txt sidecar and returns None; the callback
            lets the menubar show a quiet "too short" notice instead of
            the scary "Transcription errors" one.

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
    #
    # Broad catch is intentional: the Google API stack can raise a dozen
    # different types (HttpError, RefreshError, TransportError, OAuth
    # errors, etc.) and this enrichment is optional — we absolutely do not
    # want any of them to fail the whole pipeline for a recording that
    # transcribes + summarizes fine on its own.
    try:
        cal_meta = _calendar_enrich(wav_path)
        for key, value in cal_meta.items():
            if key not in metadata:
                metadata[key] = value
            elif key == "participants" and not metadata.get("participants"):
                metadata[key] = value
        logger.info("Calendar enrichment applied to metadata.")
    except Exception as e:  # noqa: BLE001 — best-effort enrichment
        logger.warning("Calendar enrichment failed (non-fatal): %s", e)

    # The Swift capture binary produces a single pre-mixed mic+system WAV
    # (the in-Swift MixerDrainer saturating-adds both streams into one file),
    # so transcription always reads the mic path directly. Prior versions
    # mixed a .sys.wav sibling here; that block and audio_mixer.py were
    # deleted in the Phase 4C cleanup.
    transcribe_path = wav_path

    # Step 3 & 4: Transcribe (or use pre-transcribed text from realtime mode)
    transcription = None
    if pre_transcribed_text is not None:
        from .transcriber import TranscriptionResult
        if not pre_transcribed_text:
            logger.warning(
                "Pre-transcribed text is empty — recording was under Parakeet's "
                "16-sec realtime chunk threshold. Deleting WAV + .live.txt and "
                "skipping pipeline."
            )
            # Nothing to transcribe = nothing to recover. Clean up so the
            # recording doesn't pile up in queue/ indefinitely.
            _remove_if_exists(wav_path)
            _remove_if_exists(os.path.splitext(wav_path)[0] + ".live.txt")
            if on_too_short is not None:
                try:
                    on_too_short()
                except Exception as cb_err:  # noqa: BLE001 — UI hook must never break the pipeline
                    logger.warning("on_too_short callback raised: %s", cb_err)
            return None
        logger.info("Using pre-transcribed text from realtime mode (%d chars)", len(pre_transcribed_text))
        transcription = TranscriptionResult(
            plain_text=pre_transcribed_text,
            timestamped_text=pre_transcribed_text,
            duration_minutes=0,
            srt_path="",
        )
    else:
        try:
            engine = get_batch_engine()
            logger.info("Transcribing with %s", type(engine).__name__)
            transcription = engine.transcribe(transcribe_path)
            logger.info(
                "Transcription done: %d chars, %d min",
                len(transcription.plain_text),
                transcription.duration_minutes,
            )
        except (RuntimeError, OSError, FileNotFoundError) as e:
            logger.error("Transcription failed: %s", e)
            return None

    # Step 5: Load context.md for Claude
    context_md = _load_context_md()

    # Step 6: Summarize with Claude
    #
    # summarize() itself raises RuntimeError for backend failures (both Claude
    # and Ollama retries exhausted). ValueError surfaces from the model config
    # path. We catch those and save the raw transcript without a summary —
    # strictly better than losing the recording. Unexpected programming
    # errors (TypeError, AttributeError) intentionally propagate so they're
    # not swallowed by this catch-and-continue.
    summary = None
    try:
        summary = summarize(
            transcript_text=transcription.timestamped_text,
            context_md=context_md,
            metadata=metadata,
        )
        logger.info("Summarization done: title=%r", summary.title)
        if summary.fell_back:
            logger.warning(
                "Summarizer fell back from Claude to Ollama (%s).",
                summary.model_used,
            )
            if on_summary_fallback is not None:
                try:
                    on_summary_fallback()
                except Exception as cb_err:  # noqa: BLE001 — UI hook must never break the pipeline
                    logger.warning("on_summary_fallback callback raised: %s", cb_err)
    except (RuntimeError, ValueError) as e:
        logger.error(
            "Summarization failed (will save transcript without summary): %s", e
        )

    # Step 7: Format the markdown content
    title = (summary.title if summary else None) or metadata.get(
        "title", f"{source.title()} Meeting"
    )

    # format_transcript is pure string-building; ValueError is the realistic
    # failure mode (bad date/time parse, unicode issue in a field).
    try:
        md_content = format_transcript(
            transcription=transcription,
            summary=summary,
            metadata=metadata,
            source=source,
            date_str=date_str,
            time_str=time_str,
        )
    except (ValueError, AttributeError) as e:
        logger.error("Formatter failed: %s", e)
        return None

    # Step 8: Write transcript to disk
    try:
        out_path = _write_transcript(md_content, date_str, title)
    except OSError as e:
        logger.error("Failed to write transcript file: %s", e)
        return None

    # Step 8.5: Export action items to the configured backend (best-effort).
    #
    # The transcript is already on disk — an export failure must not undo
    # it. The dispatcher itself is no-op when backend is "disabled", so
    # we always call it and let it decide. The callback only fires when a
    # backend was actually attempted (so the UI stays quiet when the
    # feature is off).
    if summary is not None:
        try:
            export_result = export_action_items(
                summary.action_items,
                metadata={
                    "title": summary.title,
                    "source": source,
                    "date_str": date_str,
                },
            )
            if export_result.attempted and on_export is not None:
                try:
                    on_export(export_result)
                except Exception as cb_err:  # noqa: BLE001 — UI hook must never break the pipeline
                    logger.warning("on_export callback raised: %s", cb_err)
        except Exception as exp_err:  # noqa: BLE001 — exporter failures are non-fatal
            logger.warning("Exporter raised unexpectedly: %s", exp_err)

    # Step 9: Update state, then clean up recording.
    #
    # Ordering matters: persist state FIRST, then touch files. If file ops
    # fail after state is durable, the recording sits in queue/ or done/ and
    # cleanup.scan_for_orphans handles it. If we did file ops first and state
    # then failed, a moved recording in done/ would have no pending_deletion
    # entry — silent leak.
    try:
        current_state = state_mod.load()
        count = current_state.get("transcripts_since_checkin", 0) + 1
        # Developer escape hatch — default is delete-after-transcribe so a
        # non-technical coworker can't stumble into the recordings folder.
        # Set MEETINGNOTES_RETAIN_RECORDINGS=1 in Engine/.env.local (gitignored)
        # to keep WAVs for debugging / regression testing.
        retain = os.environ.get("MEETINGNOTES_RETAIN_RECORDINGS") == "1"
        recording = RecordingFile(wav_path)

        state_updates: dict = {"transcripts_since_checkin": count}

        if retain:
            future_path = os.path.join(DONE_DIR, recording.basename)
            pending = list(current_state.get("pending_deletion", []))
            if not any(e.get("path") == future_path for e in pending):
                pending.append({"path": future_path, "recorded_date": date_str})
            state_updates["pending_deletion"] = pending

        state_mod.update(**state_updates)
        logger.info(
            "State updated: transcripts_since_checkin=%d, retain=%s",
            count,
            retain,
        )
    except OSError as e:
        # State-update failures (disk full, permission denied, etc.) are
        # logged but must not undo the already-written transcript. The
        # recording stays in queue/ for the next sweep.
        logger.error("Failed to update state (non-fatal): %s", e)
        return out_path

    if retain:
        try:
            recording.move_to(DONE_DIR)
            logger.info("Retained recording in %s", DONE_DIR)
        except OSError as e:
            # State already references the future done/ path. The orphan
            # scanner will reconcile if the move never completes.
            logger.error(
                "Could not move recording to %s (state already updated): %s",
                DONE_DIR, e,
            )
    else:
        recording.delete()

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
