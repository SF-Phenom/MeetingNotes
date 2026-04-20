"""FluidAudio-backed diarizer — thin subprocess wrapper around the Swift CLI.

The Swift binary (``Engine/Diarize/Sources/main.swift``, built to
``Engine/.bin/meetingnotes-diarize``) runs the actual diarization on the
Apple Neural Engine via FluidAudio's CoreML bundles. We keep Python-side
work minimal: invoke the binary, read its JSON output, map the backend's
raw speaker IDs to user-visible ``Speaker A`` / ``Speaker B`` / … labels
in first-appearance order.

Model selection is a hint: callers pass ``"community-1"`` (the default,
MIT-licensed, unlimited speakers) or ``"sortformer"`` (CC-BY-NC, capped
at 4 speakers, better DER on small meetings). An unknown value or ``None``
falls back to community-1.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile

from app.environment import DIARIZE_BIN
from app.speaker_alignment import SpeakerSegment

logger = logging.getLogger(__name__)


# Hard cap on the Swift subprocess runtime. FluidAudio community-1 reports
# ~60x real-time on ANE, so a 30-min meeting should finish in ~30s. 10 min
# is a generous "something went badly wrong, kill it" ceiling that still
# accommodates a first-run model download (which can take a couple of
# minutes on a slow connection).
DIARIZE_TIMEOUT_SECS = 600

SUPPORTED_MODELS = ("community-1", "sortformer")
DEFAULT_MODEL = "community-1"


def is_available() -> bool:
    """Return True when the Swift binary exists and is executable.

    Used by :func:`app.diarizer.get_diarizer` to auto-detect whether to
    expose this backend; also used by setup-check code so the menubar
    can tell the user why diarization is silently off.
    """
    return os.path.isfile(DIARIZE_BIN) and os.access(DIARIZE_BIN, os.X_OK)


def _label_for_index(i: int) -> str:
    """Map a zero-based speaker index to a human-readable label.

    ``0`` → ``Speaker A``, ``25`` → ``Speaker Z``, ``26`` → ``Speaker 27``
    (we stop pretending past Z — meetings with >26 distinct speakers are
    pathological and a raw number is clearer than "Speaker AA").
    """
    if i < 26:
        return f"Speaker {chr(ord('A') + i)}"
    return f"Speaker {i + 1}"


def _resolve_model(model: str | None) -> str:
    if model is None:
        return DEFAULT_MODEL
    if model not in SUPPORTED_MODELS:
        logger.warning(
            "Unknown diarizer model %r; falling back to %s.", model, DEFAULT_MODEL,
        )
        return DEFAULT_MODEL
    return model


class FluidAudioDiarizer:
    """Runs :mod:`Engine/Diarize` as a subprocess and normalizes its output."""

    def diarize(
        self, wav_path: str, model: str | None = None,
    ) -> list[SpeakerSegment] | None:
        if not is_available():
            logger.warning(
                "FluidAudio diarizer binary missing at %s — re-run setup.command.",
                DIARIZE_BIN,
            )
            return None

        resolved_model = _resolve_model(model)

        # Write the JSON output to a dedicated temp file so we never confuse
        # partial writes on subprocess crash with a previous successful run.
        tmp_fd, output_path = tempfile.mkstemp(prefix="diarize_", suffix=".json")
        os.close(tmp_fd)

        try:
            try:
                proc = subprocess.run(
                    [
                        DIARIZE_BIN,
                        "--input", wav_path,
                        "--output", output_path,
                        "--model", resolved_model,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=DIARIZE_TIMEOUT_SECS,
                )
            except subprocess.TimeoutExpired:
                logger.warning(
                    "Diarizer timed out after %ds; skipping speaker labels.",
                    DIARIZE_TIMEOUT_SECS,
                )
                return None
            except (OSError, FileNotFoundError) as e:
                logger.warning("Failed to launch diarizer: %s", e)
                return None

            if proc.returncode != 0:
                logger.warning(
                    "Diarizer subprocess failed (exit %d, model=%s): %s",
                    proc.returncode, resolved_model,
                    (proc.stderr or "").strip()[:500],
                )
                return None

            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    doc = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                logger.warning("Could not read diarizer output: %s", e)
                return None
        finally:
            try:
                os.unlink(output_path)
            except OSError:
                pass

        return _relabel(doc.get("segments", []))


def _relabel(raw_segments: list[dict]) -> list[SpeakerSegment]:
    """Convert the CLI's raw ``speaker_id`` strings to Speaker A/B/C labels.

    Ordering is by first appearance (earliest segment.start per speaker).
    So the person who spoke first becomes "Speaker A", the second new
    voice "Speaker B", and so on — matching how a reader would naturally
    assign letters while skimming the transcript.

    Segments with non-numeric or out-of-order start/end values are
    silently dropped; the Swift CLI should never produce those, but we
    tolerate them to keep a buggy build from blowing up transcription.
    """
    if not raw_segments:
        return []

    cleaned: list[tuple[float, float, str]] = []
    for seg in raw_segments:
        try:
            start = float(seg.get("start", 0.0))
            end = float(seg.get("end", 0.0))
        except (TypeError, ValueError):
            continue
        if end <= start:
            continue
        cleaned.append((start, end, str(seg.get("speaker_id", ""))))

    cleaned.sort(key=lambda t: t[0])

    id_to_label: dict[str, str] = {}
    out: list[SpeakerSegment] = []
    for start, end, raw_id in cleaned:
        if raw_id not in id_to_label:
            id_to_label[raw_id] = _label_for_index(len(id_to_label))
        out.append(
            SpeakerSegment(start=start, end=end, speaker=id_to_label[raw_id])
        )
    return out
