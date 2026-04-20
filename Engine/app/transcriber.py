"""
Transcriber — Parakeet transcription engine for MeetingNotes.

Uses parakeet-mlx (Apple Silicon native via MLX) for on-device transcription.
Returns a TranscriptionResult with plain text, timestamped text, and duration.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)


# --- Data types --------------------------------------------------------------

@dataclass
class TranscriptionResult:
    plain_text: str           # Full text without timestamps
    timestamped_text: str     # Text with [HH:MM:SS] prefixes
    duration_minutes: int     # Meeting duration in minutes (rounded)
    srt_path: str             # Path to the .srt file (kept for reference)


# --- Parakeet engine ---------------------------------------------------------

PARAKEET_MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v3"
PARAKEET_BEAM_SIZE = 8

# Maximum chunk duration (seconds) for batch Parakeet transcription.
# Keeps GPU memory bounded on 16 GB unified-memory machines.
PARAKEET_CHUNK_SECS = 300  # 5 minutes

# Hard cap on GPU memory MLX is allowed to use (bytes).
# Leaves headroom for macOS, the app, and other processes.
MLX_MEMORY_LIMIT = 6 * 1024 * 1024 * 1024  # 6 GB

# Diagnostic watchdog: if mx.synchronize() blocks longer than this we log
# loudly. The call itself is a C extension and can't be cancelled from Python,
# so the watchdog is observational — its value is (1) making wedged-GPU
# symptoms obvious in logs instead of the app silently freezing, and (2)
# letting realtime callers mark the session unhealthy so future cycles abort.
GPU_SYNC_WATCHDOG_SECS = 60

_parakeet_model = None  # lazy-loaded, stays in memory for reuse
_parakeet_stream = None  # reusable MLX GPU stream


def synchronize_with_watchdog(
    stream,
    name: str = "mlx",
    on_timeout: Callable[[], None] | None = None,
    timeout_secs: float = GPU_SYNC_WATCHDOG_SECS,
) -> None:
    """Call mx.synchronize(stream) with a diagnostic watchdog.

    If the call blocks longer than ``timeout_secs``, log an error and invoke
    ``on_timeout`` (if provided) so a higher-level session can mark itself
    unhealthy. mx.synchronize is a blocking C call — we cannot interrupt it.
    """
    import mlx.core as mx

    fired = threading.Event()

    def _timeout_cb() -> None:
        fired.set()
        logger.error(
            "%s: mx.synchronize has been blocked for >%.0fs — GPU may be "
            "wedged. Check Metal diagnostics in Console.app.",
            name,
            timeout_secs,
        )
        if on_timeout is not None:
            try:
                on_timeout()
            except Exception as exc:  # noqa: BLE001 — best-effort
                logger.error("Watchdog on_timeout callback failed: %s", exc)

    timer = threading.Timer(timeout_secs, _timeout_cb)
    timer.daemon = True
    timer.start()
    try:
        mx.synchronize(stream)
    finally:
        timer.cancel()


def _load_parakeet_model():
    """Load the parakeet-mlx model and create a reusable GPU stream."""
    global _parakeet_model, _parakeet_stream
    if _parakeet_model is None:
        import mlx.core as mx
        # Cap GPU memory so Parakeet can't starve the system
        mx.metal.set_memory_limit(MLX_MEMORY_LIMIT)
        logger.info(
            "MLX GPU memory limit set to %.1f GB",
            MLX_MEMORY_LIMIT / (1024 ** 3),
        )
        logger.info("Loading parakeet-mlx model (first call, ~2.5 GB download on first use)...")
        from parakeet_mlx import from_pretrained
        _parakeet_model = from_pretrained(PARAKEET_MODEL_ID)
        _parakeet_stream = mx.new_stream(mx.gpu)
        logger.info("Parakeet model loaded")
    return _parakeet_model


def cleanup_parakeet() -> None:
    """Release the cached Parakeet model and clear MLX GPU memory.

    Called from the menubar Quit handler so the ~2.5 GB of GPU memory
    held by the model is released cleanly before the process exits.
    Python would reclaim it on exit anyway, but doing it explicitly
    means the Metal driver sees a clean teardown — useful when the user
    quits and immediately relaunches.

    Idempotent — safe to call when no model has been loaded.
    """
    global _parakeet_model, _parakeet_stream
    if _parakeet_model is None and _parakeet_stream is None:
        return
    _parakeet_model = None
    _parakeet_stream = None
    try:
        import mlx.core as mx
        mx.metal.clear_cache()
        logger.info("Parakeet model released and MLX cache cleared")
    except Exception as e:  # noqa: BLE001 — cleanup must never crash quit
        logger.warning("Could not clear MLX cache during cleanup: %s", e)


def _transcribe_chunk(model, wav_chunk_path: str, decoding) -> object:
    """Transcribe a single WAV chunk and free GPU memory afterward."""
    import mlx.core as mx
    with mx.stream(_parakeet_stream):
        result = model.transcribe(wav_chunk_path, decoding_config=decoding)
    synchronize_with_watchdog(_parakeet_stream, name="parakeet-batch")
    mx.metal.clear_cache()
    return result


def transcribe_with_parakeet(wav_path: str) -> TranscriptionResult:
    """
    Transcribe a .wav file using parakeet-mlx.

    Long recordings are split into chunks of PARAKEET_CHUNK_SECS to keep
    GPU memory bounded and avoid freezing the system.

    Args:
        wav_path: Absolute path to the .wav file.

    Returns:
        TranscriptionResult with plain text, timestamped text, and duration.

    Raises:
        FileNotFoundError: If the .wav file does not exist.
        RuntimeError: If parakeet fails or produces no output.
    """
    import wave as wave_mod
    import tempfile

    wav_path = os.path.expanduser(wav_path)

    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"WAV file not found: {wav_path}")

    logger.info("Starting Parakeet transcription: %s", os.path.basename(wav_path))
    t0 = time.time()

    # Read WAV metadata to determine if chunking is needed
    try:
        with wave_mod.open(wav_path, "rb") as wf:
            n_channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
    except Exception as e:
        raise RuntimeError(f"Could not read WAV file: {e}") from e

    duration_secs = n_frames / framerate
    chunk_frames = int(PARAKEET_CHUNK_SECS * framerate)

    try:
        import mlx.core as mx
        from parakeet_mlx.parakeet import DecodingConfig, Beam
        decoding = DecodingConfig(decoding=Beam(beam_size=PARAKEET_BEAM_SIZE))
        model = _load_parakeet_model()
    except ImportError:
        raise RuntimeError(
            "parakeet-mlx is not installed. Run: pip install parakeet-mlx"
        )

    all_sentences = []
    last_end_seconds = 0.0

    if duration_secs <= PARAKEET_CHUNK_SECS:
        # Short recording — transcribe in one pass
        logger.info("Recording is %.0fs, transcribing in one pass", duration_secs)
        try:
            result = _transcribe_chunk(model, wav_path, decoding)
        except Exception as e:
            raise RuntimeError(f"Parakeet transcription failed: {e}") from e

        all_sentences, last_end_seconds = _extract_sentences(result, time_offset=0)
    else:
        # Long recording — split into chunks
        n_chunks = int((n_frames + chunk_frames - 1) // chunk_frames)
        logger.info(
            "Recording is %.0fs, splitting into %d chunks of %ds each",
            duration_secs, n_chunks, PARAKEET_CHUNK_SECS,
        )

        with wave_mod.open(wav_path, "rb") as wf:
            for chunk_idx in range(n_chunks):
                offset_frames = chunk_idx * chunk_frames
                frames_to_read = min(chunk_frames, n_frames - offset_frames)
                time_offset = offset_frames / framerate

                wf.setpos(offset_frames)
                pcm_data = wf.readframes(frames_to_read)

                # Write chunk to a temporary WAV file
                tmp_path = None
                try:
                    tmp_fd = tempfile.NamedTemporaryFile(
                        suffix=".wav", delete=False,
                        dir=os.path.dirname(wav_path),
                    )
                    tmp_path = tmp_fd.name
                    with wave_mod.open(tmp_path, "wb") as chunk_wf:
                        chunk_wf.setnchannels(n_channels)
                        chunk_wf.setsampwidth(sample_width)
                        chunk_wf.setframerate(framerate)
                        chunk_wf.writeframes(pcm_data)

                    chunk_t0 = time.time()
                    logger.info(
                        "Transcribing chunk %d/%d (%.0fs–%.0fs)",
                        chunk_idx + 1, n_chunks,
                        time_offset, time_offset + frames_to_read / framerate,
                    )
                    result = _transcribe_chunk(model, tmp_path, decoding)
                    chunk_elapsed = time.time() - chunk_t0
                    logger.info(
                        "Chunk %d/%d done in %.1fs",
                        chunk_idx + 1, n_chunks, chunk_elapsed,
                    )

                    chunk_sents, chunk_end = _extract_sentences(
                        result, time_offset=time_offset,
                    )
                    all_sentences.extend(chunk_sents)
                    last_end_seconds = max(last_end_seconds, chunk_end)

                except Exception as e:
                    logger.error(
                        "Parakeet chunk %d/%d failed: %s",
                        chunk_idx + 1, n_chunks, e,
                    )
                    # Continue with remaining chunks rather than aborting
                finally:
                    if tmp_path and os.path.exists(tmp_path):
                        try:
                            os.unlink(tmp_path)
                        except OSError:
                            pass

                # Release the pcm_data buffer immediately
                del pcm_data

    elapsed = time.time() - t0

    if not all_sentences:
        raise RuntimeError("Parakeet produced no output")

    duration_minutes = max(1, round(last_end_seconds / 60))

    # Apply the repeat-collapsing filter + corrections dictionary, then
    # hand the resulting sentences to the paragraph formatter. The filter
    # works on (key, text) tuples — use the list index as the key so we
    # can recover each kept sentence's original start/end timestamps.
    from app.transcript_filter import filter_segments
    from app.corrections import apply_corrections
    from app.transcript_formatter import (
        Sentence,
        build_plain_paragraphs,
        build_timestamped_paragraphs,
    )
    indexed = [(str(i), s.text) for i, s in enumerate(all_sentences)]
    filtered = filter_segments(indexed)
    filtered_sentences: list[Sentence] = []
    for idx_str, text in filtered:
        orig = all_sentences[int(idx_str)]
        filtered_sentences.append(
            Sentence(
                start=orig.start,
                end=orig.end,
                text=apply_corrections(text),
                speech_end=orig.speech_end,
            )
        )

    # Optional diarization: if the user opted in AND a backend is available,
    # assign Speaker A / Speaker B / … labels to each sentence by max time
    # overlap with the diarizer's segments. Any failure here is logged and
    # swallowed — we'd rather ship a transcript without speaker labels than
    # lose one over a diarization bug.
    filtered_sentences = _diarize_if_enabled(wav_path, filtered_sentences)

    timestamped_text = build_timestamped_paragraphs(filtered_sentences)
    plain_text = build_plain_paragraphs(filtered_sentences)

    logger.info(
        "Parakeet transcription complete: %d segments, ~%d min, %.1fs wall time",
        len(filtered_sentences),
        duration_minutes,
        elapsed,
    )

    return TranscriptionResult(
        plain_text=plain_text,
        timestamped_text=timestamped_text,
        duration_minutes=duration_minutes,
        srt_path="",  # Parakeet doesn't produce SRT files
    )


def _extract_sentences(
    result, time_offset: float = 0,
):
    """Extract :class:`Sentence` records from a Parakeet AlignedResult.

    Adds ``time_offset`` so sentences from later chunks line up on the
    recording's absolute timeline. Returns ``(sentences, last_end)``.
    """
    from app.transcript_formatter import Sentence

    sentences: list[Sentence] = []
    last_end = 0.0

    for sent in result.sentences:
        text = sent.text.strip()
        if not text:
            continue
        abs_start = sent.start + time_offset
        abs_end = sent.end + time_offset
        sentences.append(
            Sentence(
                start=abs_start,
                end=abs_end,
                text=text,
                speech_end=_speech_end(sent, time_offset),
            )
        )
        last_end = max(last_end, abs_end)

    return sentences, last_end


# Pure-punctuation tokens whose duration is mostly silence Parakeet absorbs
# rather than spoken audio. Skipping them when computing ``speech_end``
# gives us the end-of-real-speech reference needed for pause detection.
_PARAKEET_PURE_PUNCT = frozenset(".!?,;:")


def _speech_end(sent, time_offset: float) -> float:
    """End time of the last non-pure-punctuation token in ``sent``."""
    for tok in reversed(sent.tokens):
        if tok.text.strip() not in _PARAKEET_PURE_PUNCT:
            return tok.end + time_offset
    # Degenerate case: whole sentence is punctuation. Fall back to sent.end.
    return sent.end + time_offset


# Env var override for the diarization_enabled state flag. Accepts the
# usual truthy strings ("1", "true", "yes", "on") case-insensitively;
# anything else (including unset) defers to state.json. Lets developers
# exercise the diarization path without having to edit state.json by hand.
DIARIZATION_ENABLED_ENV_VAR = "MEETINGNOTES_DIARIZATION"


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
        logger.warning("Could not read diarization flag from state (%s); treating as off.", e)
        return False


def _diarize_if_enabled(wav_path: str, sentences):
    """Assign speaker labels in-place when diarization is enabled and available.

    Returns the input list unchanged when diarization is off, when no
    backend is registered, or when the backend fails. Never raises —
    speaker labels are a nice-to-have and must not break transcription.
    """
    if not _diarization_enabled():
        return sentences
    try:
        from app.diarizer import get_diarizer
        diarizer = get_diarizer()
        if diarizer is None:
            logger.info("Diarization enabled but no backend registered; skipping.")
            return sentences
        segments = diarizer.diarize(wav_path)
        if not segments:
            logger.info("Diarizer returned no segments for %s.", os.path.basename(wav_path))
            return sentences
        from app.speaker_alignment import assign_speakers
        labelled = assign_speakers(sentences, segments)
        n_labelled = sum(1 for s in labelled if s.speaker is not None)
        logger.info(
            "Diarization assigned speakers to %d/%d sentences (%d segments).",
            n_labelled, len(labelled), len(segments),
        )
        return labelled
    except Exception as e:  # noqa: BLE001 — diarization failure is non-fatal
        logger.warning("Diarization failed (non-fatal), continuing without speakers: %s", e)
        return sentences
