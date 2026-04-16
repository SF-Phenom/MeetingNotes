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

    all_segments: list[tuple[str, str]] = []
    last_end_seconds = 0

    if duration_secs <= PARAKEET_CHUNK_SECS:
        # Short recording — transcribe in one pass
        logger.info("Recording is %.0fs, transcribing in one pass", duration_secs)
        try:
            result = _transcribe_chunk(model, wav_path, decoding)
        except Exception as e:
            raise RuntimeError(f"Parakeet transcription failed: {e}") from e

        all_segments, last_end_seconds = _extract_segments(result, time_offset=0)
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

                    chunk_segs, chunk_end = _extract_segments(
                        result, time_offset=time_offset,
                    )
                    all_segments.extend(chunk_segs)
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

    if not all_segments:
        raise RuntimeError("Parakeet produced no output")

    duration_minutes = max(1, round(last_end_seconds / 60))

    # Apply transcript filter and corrections dictionary
    from app.transcript_filter import filter_segments
    from app.corrections import apply_corrections
    all_segments = filter_segments(all_segments)
    all_segments = [(ts, apply_corrections(txt)) for ts, txt in all_segments]
    timestamped_lines = [f"{ts} {txt}" for ts, txt in all_segments]
    timestamped_text = "\n".join(timestamped_lines)
    plain_text = " ".join(txt for _, txt in all_segments)

    logger.info(
        "Parakeet transcription complete: %d segments, ~%d min, %.1fs wall time",
        len(all_segments),
        duration_minutes,
        elapsed,
    )

    return TranscriptionResult(
        plain_text=plain_text,
        timestamped_text=timestamped_text,
        duration_minutes=duration_minutes,
        srt_path="",  # Parakeet doesn't produce SRT files
    )


def _extract_segments(
    result, time_offset: float = 0,
) -> tuple[list[tuple[str, str]], float]:
    """
    Extract (timestamp, text) segments from a Parakeet AlignedResult.

    Adds time_offset to all timestamps so chunks are stitched correctly.
    Returns (segments, last_end_seconds).
    """
    segments: list[tuple[str, str]] = []
    last_end = 0.0

    for sent in result.sentences:
        seg_text = sent.text.strip()
        if not seg_text:
            continue
        abs_start = sent.start + time_offset
        abs_end = sent.end + time_offset
        h = int(abs_start // 3600)
        m = int((abs_start % 3600) // 60)
        s = int(abs_start % 60)
        timestamp = f"[{h:02d}:{m:02d}:{s:02d}]"
        segments.append((timestamp, seg_text))
        last_end = max(last_end, abs_end)

    return segments, last_end
