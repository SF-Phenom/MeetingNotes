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


# --- Diarization -------------------------------------------------------------
#
# Runs after transcription produces the TranscriptionResult, regardless of
# whether sentences came from realtime (fast path) or the batch engine
# (realtime unavailable or Apple Speech). We align speaker segments to
# sentences, then re-render ``plain_text`` and ``timestamped_text`` so the
# written .md carries ``**Speaker A:**`` prefixes at paragraph boundaries.
#
# Critically, this does NOT re-read the WAV for transcription. Diarization
# itself reads the WAV once (inside the FluidAudio subprocess) but that's
# it — the retention posture of "realtime streams chunks and discards" is
# preserved: no second full-file transcription pass is ever introduced by
# enabling diarization.


# Env var override for the diarization_enabled state flag. Accepts the
# usual truthy strings ("1", "true", "yes", "on") case-insensitively;
# anything else (including unset) defers to state.json.
DIARIZATION_ENABLED_ENV_VAR = "MEETINGNOTES_DIARIZATION"

# Sortformer caps out at 4 speakers; community-1 has no cap. Threshold
# is total calendar headcount (user + other attendees). We route up to
# 6 scheduled attendees to sortformer even though the model only emits
# 4 tracks: meetings with >3 invitees routinely have 1–2 no-shows, so
# ≤6 on the calendar usually means ≤4 actually join. When more than 4
# do join, sortformer bundles the extras under an existing label —
# acceptable degradation for the DER gain. Unknown count or >6 routes
# to community-1.
_SORTFORMER_MAX_PARTICIPANTS = 6


def _diarization_enabled() -> bool:
    """Resolve the diarization flag: env var override > state.json > default."""
    env = os.environ.get(DIARIZATION_ENABLED_ENV_VAR, "").strip().lower()
    if env in ("1", "true", "yes", "on"):
        return True
    if env in ("0", "false", "no", "off"):
        return False
    try:
        from app.state import State
        return State.load().diarization_enabled
    except Exception as e:  # noqa: BLE001 — diarization must never break transcription
        logger.warning(
            "Could not read diarization flag from state (%s); treating as off.", e,
        )
        return False


def _pick_diarizer_model(metadata: dict) -> str:
    """Pick between FluidAudio's community-1 and sortformer backends.

    Sortformer is the default; community-1 only takes over when the
    calendar tells us the meeting is large (>6 total attendees).
    Rationale: ad-hoc recordings with no matching calendar event are
    typically 1:1s, standups, or small syncs, so sortformer's better
    DER is the right bet. Large unscheduled gatherings exist but are
    rare, and the cluster-bundling degradation is the same one we
    already tolerate for scheduled >4-speaker meetings routed to
    sortformer under the headcount rule above.

    ``metadata["participants"]`` is a ", "-joined display-name string of
    OTHER attendees — the user is filtered out upstream by
    ``calendar_lookup``, so we add 1 to the count to get total headcount.
    """
    participants = metadata.get("participants")
    if isinstance(participants, str) and participants.strip():
        n_others = len([p for p in participants.split(",") if p.strip()])
        total = n_others + 1  # +1 for the user
        if total > _SORTFORMER_MAX_PARTICIPANTS:
            return "community-1"
    return "sortformer"


def _maybe_diarize(wav_path, transcription, metadata):
    """Run speaker diarization on ``transcription.sentences`` when enabled.

    Returns a new ``TranscriptionResult`` with speakered text, or the
    input unchanged when diarization is off / no backend is registered /
    no sentences are available / the diarizer failed. Never raises —
    speaker labels are a nice-to-have and must not break transcription.
    """
    if not _diarization_enabled():
        return transcription

    sentences = transcription.sentences
    if not sentences:
        # Apple Speech path (no timings) or a too-short recording. Quietly
        # skip — diarization isn't applicable.
        logger.debug(
            "Diarization enabled but no sentences available for %s; skipping.",
            os.path.basename(wav_path),
        )
        return transcription

    try:
        from app.diarizer import get_diarizer
        diarizer = get_diarizer()
        if diarizer is None:
            logger.info(
                "Diarization enabled but no backend registered; skipping.",
            )
            return transcription

        model = _pick_diarizer_model(metadata)
        logger.info(
            "Running diarizer (model=%s) on %s",
            model, os.path.basename(wav_path),
        )
        segments = diarizer.diarize(wav_path, model=model)
        if not segments:
            logger.info(
                "Diarizer returned no segments for %s.",
                os.path.basename(wav_path),
            )
            return transcription

        from app.speaker_alignment import assign_speakers
        from app.transcript_formatter import (
            build_plain_paragraphs,
            build_timestamped_paragraphs,
        )
        labelled = assign_speakers(sentences, segments)
        n_labelled = sum(1 for s in labelled if s.speaker is not None)
        logger.info(
            "Diarization assigned speakers to %d/%d sentences (%d segments).",
            n_labelled, len(labelled), len(segments),
        )

        # Rebuild text from the speakered sentences so the .md transcript
        # picks up **Speaker X:** prefixes on paragraph boundaries.
        from app.transcriber import TranscriptionResult
        return TranscriptionResult(
            plain_text=build_plain_paragraphs(labelled),
            timestamped_text=build_timestamped_paragraphs(labelled),
            duration_minutes=transcription.duration_minutes,
            srt_path=transcription.srt_path,
            sentences=labelled,
        )
    except Exception as e:  # noqa: BLE001 — diarization failure is non-fatal
        logger.warning(
            "Diarization failed (non-fatal), continuing without speakers: %s", e,
        )
        return transcription


def _resolve_base_title(metadata: dict, summary, source: str) -> str:
    """Pick the working title for a transcript, preferring calendar over LLM.

    Priority:
      1. Calendar event title (via metadata['title'], populated when enrichment
         associated an event — see calendar_lookup.lookup_meeting's strict
         association rule).
      2. LLM-derived summary title.
      3. Source-based fallback ("Zoom Meeting", "Manual Meeting", etc.).
    """
    cal_title = metadata.get("title")
    if cal_title:
        return cal_title
    if summary is not None and summary.title:
        return summary.title
    return f"{source.title()} Meeting"


def _find_prior_parts(
    event_id: str,
    date_str: str,
    transcripts_root: str,
) -> list[str]:
    """Return paths of transcripts already on disk for the same calendar event
    on the same day. Used to number subsequent recordings "(part 2)", "(part 3)".

    Scan scope: transcripts/YYYY/MM/ with filenames prefixed by date_str. We
    read only the frontmatter (first ~2KB) to look for a matching
    ``calendar_event_id:`` line — cheap, and the field is always near the top.
    Empty event_id short-circuits to an empty list.
    """
    if not event_id:
        return []
    try:
        year, month, _ = date_str.split("-")
    except ValueError:
        return []

    day_dir = os.path.join(transcripts_root, year, month)
    if not os.path.isdir(day_dir):
        return []

    needle = f"calendar_event_id: {event_id}"
    prior: list[str] = []
    try:
        entries = os.listdir(day_dir)
    except OSError:
        return []
    for filename in entries:
        if not filename.startswith(f"{date_str}_") or not filename.endswith(".md"):
            continue
        path = os.path.join(day_dir, filename)
        try:
            with open(path, "r", encoding="utf-8") as f:
                head = f.read(2048)
        except OSError:
            continue
        if needle in head:
            prior.append(path)
    return prior


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
    pre_transcribed_sentences: list | None = None,
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
        pre_transcribed_sentences: Optional list of :class:`Sentence`
            records carried over from the realtime engine. When present
            (Parakeet realtime only; Apple Speech can't produce timings),
            the pipeline can run speaker diarization on them without
            re-reading the WAV. Safe to pass ``None`` or ``[]`` — the
            pipeline simply skips the diarization step.
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
            # Carry realtime-produced sentences through so the diarization
            # step can align speaker labels without a second WAV read.
            sentences=pre_transcribed_sentences or None,
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

    # Step 4b: Optional speaker diarization. Runs on whichever sentence
    # list is available (realtime's accumulated or batch engine's output),
    # re-renders plain_text and timestamped_text with **Speaker A:**
    # prefixes on paragraph boundaries. No-op when diarization is off,
    # when no backend is registered, or when no sentences are available
    # (Apple Speech path).
    transcription = _maybe_diarize(wav_path, transcription, metadata)

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
    #
    # Title priority: calendar event title > LLM summary title > source
    # fallback. If an earlier transcript on disk already claims this same
    # calendar event for today, tag this one "(part N)" so both titles
    # stay human-readable instead of colliding into an HHMMSS suffix.
    base_title = _resolve_base_title(metadata, summary, source)
    event_id = metadata.get("calendar_event_id", "")
    prior_parts = _find_prior_parts(event_id, date_str, TRANSCRIPTS_DIR)
    if prior_parts:
        title = f"{base_title} (part {len(prior_parts) + 1})"
    else:
        title = base_title
    # Formatter reads metadata["title"] as the authoritative display title.
    metadata["title"] = title

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
