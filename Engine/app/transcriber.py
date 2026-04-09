"""
Transcriber — transcription engines for MeetingNotes.

Supports two engines:
  - whisper: Calls the local whisper-cli binary (whisper.cpp)
  - parakeet: Uses parakeet-mlx (Apple Silicon native via MLX)

Both return a TranscriptionResult with the same shape.
"""

from __future__ import annotations

import os
import re
import logging
import subprocess
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --- Configuration -----------------------------------------------------------

from app.state import CONTEXT_PATH

WHISPER_DIR = os.environ.get("WHISPER_DIR", os.path.expanduser("~/whisper.cpp"))
WHISPER_BINARY = os.path.join(WHISPER_DIR, "build", "bin", "whisper-cli")
WHISPER_MODEL = os.path.join(WHISPER_DIR, "models", "ggml-large-v3-turbo.bin")
VAD_MODEL = os.path.join(WHISPER_DIR, "models", "for-tests-silero-v6.2.0-ggml.bin")


# --- Data types --------------------------------------------------------------

@dataclass
class TranscriptionResult:
    plain_text: str           # Full text without timestamps
    timestamped_text: str     # Text with [HH:MM:SS] prefixes
    duration_minutes: int     # Meeting duration in minutes (rounded)
    srt_path: str             # Path to the .srt file (kept for reference)


# --- Helpers -----------------------------------------------------------------

def build_initial_prompt() -> str:
    """
    Read context.md and extract names and domain terms to bias whisper's
    transcription toward correct spellings.

    Extracts:
    - Text between **...** markers (typically names)
    - Terms listed under a "## Domain Terminology" section
    """
    try:
        with open(CONTEXT_PATH, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        logger.warning("context.md not found at %s — no initial prompt", CONTEXT_PATH)
        return ""
    except OSError as e:
        logger.warning("Could not read context.md: %s", e)
        return ""

    terms: list[str] = []

    # Extract all **bold** markers — these are names like **John Deal**
    bold_terms = re.findall(r"\*\*([^*]+)\*\*", content)
    for term in bold_terms:
        term = term.strip()
        if term:
            terms.append(term)

    # Also extract parenthetical "referred to as" aliases
    # e.g. (also referred to as "Jo")
    alias_terms = re.findall(r'referred to as ["\u201c]([^"\u201d]+)["\u201d]', content)
    for term in alias_terms:
        term = term.strip()
        if term and term not in terms:
            terms.append(term)

    # Extract items from a "## Domain Terminology" section if present
    domain_match = re.search(
        r"##\s+Domain Terminology\s*\n(.*?)(?=\n##|\Z)", content, re.DOTALL
    )
    if domain_match:
        domain_block = domain_match.group(1)
        # Each line that starts with - or a word
        for line in domain_block.splitlines():
            line = line.strip().lstrip("-•*").strip()
            if line:
                terms.append(line)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_terms: list[str] = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique_terms.append(t)

    prompt = ", ".join(unique_terms)
    logger.debug("Built initial prompt (%d terms): %s", len(unique_terms), prompt[:120])
    return prompt


def _parse_srt(srt_path: str) -> tuple[list[tuple[str, str]], int]:
    """
    Parse an SRT file into (timestamp, text) pairs and total duration in minutes.

    SRT format:
        1
        00:00:12,340 --> 00:00:14,820
        Hello everyone.

    Returns:
        segments: list of ("[HH:MM:SS]", "segment text") tuples
        duration_minutes: int, duration of the last segment end time in minutes
    """
    segments: list[tuple[str, str]] = []
    last_end_seconds = 0

    try:
        with open(srt_path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        logger.error("Could not read SRT file %s: %s", srt_path, e)
        return segments, 0

    # SRT blocks are separated by blank lines
    blocks = re.split(r"\n\s*\n", raw.strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue

        # Line 0: sequence number (skip)
        # Line 1: timestamps "HH:MM:SS,mmm --> HH:MM:SS,mmm"
        # Line 2+: text
        time_line = lines[1]
        time_match = re.match(
            r"(\d{2}):(\d{2}):(\d{2}),\d+ --> (\d{2}):(\d{2}):(\d{2}),\d+",
            time_line,
        )
        if not time_match:
            continue

        start_h, start_m, start_s = (
            int(time_match.group(1)),
            int(time_match.group(2)),
            int(time_match.group(3)),
        )
        end_h, end_m, end_s = (
            int(time_match.group(4)),
            int(time_match.group(5)),
            int(time_match.group(6)),
        )

        timestamp_label = f"[{start_h:02d}:{start_m:02d}:{start_s:02d}]"
        segment_text = " ".join(line.strip() for line in lines[2:] if line.strip())

        if segment_text:
            segments.append((timestamp_label, segment_text))

        last_end_seconds = end_h * 3600 + end_m * 60 + end_s

    duration_minutes = max(1, round(last_end_seconds / 60))
    return segments, duration_minutes


# --- Public API --------------------------------------------------------------

def transcribe(wav_path: str, initial_prompt: str | None = None) -> TranscriptionResult:
    """
    Transcribe a .wav file using the local whisper.cpp binary.

    Args:
        wav_path: Absolute path to the .wav file.
        initial_prompt: Optional string of names/terms to seed whisper's
                        vocabulary (improves spelling of proper nouns).

    Returns:
        TranscriptionResult with plain text, timestamped text, duration, and
        path to the .srt file.

    Raises:
        FileNotFoundError: If the .wav file does not exist.
        RuntimeError: If whisper.cpp fails or produces no output.
    """
    wav_path = os.path.expanduser(wav_path)

    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"WAV file not found: {wav_path}")

    if not os.path.exists(WHISPER_BINARY):
        raise FileNotFoundError(
            f"whisper-cli binary not found at {WHISPER_BINARY}. "
            "Run the build step described in SETUP.md."
        )

    if not os.path.exists(WHISPER_MODEL):
        raise FileNotFoundError(
            f"Whisper model not found at {WHISPER_MODEL}. "
            "Download it as described in SETUP.md."
        )

    basename = os.path.splitext(wav_path)[0]
    txt_path = basename + ".txt"
    srt_path = basename + ".srt"

    cmd = [
        WHISPER_BINARY,
        "-m", WHISPER_MODEL,
        "-f", wav_path,
        "-of", basename,           # output file base (without extension)
        "--output-srt",
        "--output-txt",
        "-l", "en",
        "--print-progress",
        "-pp",
        "--vad",                   # Voice Activity Detection — skip silence
        "--vad-model", VAD_MODEL,
        "--suppress-nst",          # suppress non-speech tokens
        "--entropy-thold", "2.0",  # stricter quality gate (default 2.4)
    ]
    if initial_prompt:
        cmd += ["--prompt", initial_prompt]

    logger.info("Starting whisper transcription: %s", os.path.basename(wav_path))
    logger.debug("whisper command: %s", " ".join(cmd))
    t0 = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1-hour hard limit; long meetings are possible
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("whisper.cpp timed out after 1 hour")
    except OSError as e:
        raise RuntimeError(f"Failed to launch whisper-cli: {e}") from e

    elapsed = time.time() - t0
    logger.info(
        "whisper finished in %.1fs (exit code %d)", elapsed, result.returncode
    )

    if result.returncode != 0:
        logger.error("whisper stderr:\n%s", result.stderr[-2000:])
        raise RuntimeError(
            f"whisper-cli exited with code {result.returncode}. "
            f"stderr tail: {result.stderr[-500:]}"
        )

    # Read plain text output
    if not os.path.exists(txt_path):
        raise RuntimeError(
            f"whisper did not produce a .txt file at {txt_path}"
        )

    try:
        with open(txt_path, "r", encoding="utf-8") as f:
            plain_text = f.read().strip()
    except OSError as e:
        raise RuntimeError(f"Could not read whisper .txt output: {e}") from e
    finally:
        # Clean up .txt regardless — we keep SRT for reference
        try:
            os.remove(txt_path)
            logger.debug("Cleaned up .txt file: %s", txt_path)
        except OSError as e:
            logger.warning("Could not remove .txt file %s: %s", txt_path, e)

    # Parse SRT for timestamps and duration
    if not os.path.exists(srt_path):
        logger.warning("whisper did not produce a .srt file at %s", srt_path)
        # Fall back: return plain text with no timestamps
        return TranscriptionResult(
            plain_text=plain_text,
            timestamped_text=plain_text,
            duration_minutes=0,
            srt_path="",
        )

    segments, duration_minutes = _parse_srt(srt_path)

    if segments:
        from app.transcript_filter import filter_segments
        segments = filter_segments(segments)
        timestamped_lines = [f"{ts} {text}" for ts, text in segments]
        timestamped_text = "\n".join(timestamped_lines)
        # Rebuild plain_text from filtered segments so the summarizer gets clean input
        plain_text = " ".join(text for _, text in segments)
    else:
        logger.warning("SRT parsed 0 segments from %s", srt_path)
        timestamped_text = plain_text

    logger.info(
        "Transcription complete: %d segments, ~%d min, %.1fs wall time",
        len(segments),
        duration_minutes,
        elapsed,
    )

    return TranscriptionResult(
        plain_text=plain_text,
        timestamped_text=timestamped_text,
        duration_minutes=duration_minutes,
        srt_path=srt_path,
    )


# --- Parakeet engine ---------------------------------------------------------

PARAKEET_MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v3"
PARAKEET_BEAM_SIZE = 8

# Maximum chunk duration (seconds) for batch Parakeet transcription.
# Keeps GPU memory bounded on 16 GB unified-memory machines.
PARAKEET_CHUNK_SECS = 300  # 5 minutes

# Hard cap on GPU memory MLX is allowed to use (bytes).
# Leaves headroom for macOS, the app, and other processes.
MLX_MEMORY_LIMIT = 6 * 1024 * 1024 * 1024  # 6 GB

_parakeet_model = None  # lazy-loaded, stays in memory for reuse
_parakeet_stream = None  # reusable MLX GPU stream


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
    mx.synchronize(_parakeet_stream)
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
        TranscriptionResult with the same shape as the whisper path.

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

    # Apply the same transcript filter used for whisper output
    from app.transcript_filter import filter_segments
    all_segments = filter_segments(all_segments)
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
