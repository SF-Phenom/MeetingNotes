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

from app.state import BASE_DIR

WHISPER_BINARY = os.path.expanduser("~/whisper.cpp/build/bin/whisper-cli")
WHISPER_MODEL = os.path.expanduser("~/whisper.cpp/models/ggml-large-v3-turbo.bin")
VAD_MODEL = os.path.expanduser("~/whisper.cpp/models/for-tests-silero-v6.2.0-ggml.bin")
CONTEXT_PATH = os.path.join(BASE_DIR, "Settings", "context.md")


# --- Data types --------------------------------------------------------------

@dataclass
class TranscriptionResult:
    plain_text: str           # Full text without timestamps
    timestamped_text: str     # Text with [HH:MM:SS] prefixes
    duration_minutes: int     # Meeting duration in minutes (rounded)
    srt_path: str             # Path to the .srt file (kept for reference)


# --- Helpers -----------------------------------------------------------------

def _build_initial_prompt() -> str:
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

_parakeet_model = None  # lazy-loaded, stays in memory for reuse


def _load_parakeet_model():
    """Load the parakeet-mlx model (lazy, cached across calls)."""
    global _parakeet_model
    if _parakeet_model is None:
        logger.info("Loading parakeet-mlx model (first call, ~2.5 GB download on first use)...")
        from parakeet_mlx import from_pretrained
        _parakeet_model = from_pretrained(PARAKEET_MODEL_ID)
        logger.info("Parakeet model loaded")
    return _parakeet_model


def transcribe_with_parakeet(wav_path: str) -> TranscriptionResult:
    """
    Transcribe a .wav file using parakeet-mlx.

    Args:
        wav_path: Absolute path to the .wav file.

    Returns:
        TranscriptionResult with the same shape as the whisper path.

    Raises:
        FileNotFoundError: If the .wav file does not exist.
        RuntimeError: If parakeet fails or produces no output.
    """
    wav_path = os.path.expanduser(wav_path)

    if not os.path.exists(wav_path):
        raise FileNotFoundError(f"WAV file not found: {wav_path}")

    logger.info("Starting Parakeet transcription: %s", os.path.basename(wav_path))
    t0 = time.time()

    try:
        from parakeet_mlx.parakeet import DecodingConfig, Beam
        decoding = DecodingConfig(decoding=Beam(beam_size=PARAKEET_BEAM_SIZE))
        model = _load_parakeet_model()
        result = model.transcribe(wav_path, decoding_config=decoding)
    except ImportError:
        raise RuntimeError(
            "parakeet-mlx is not installed. Run: pip install parakeet-mlx"
        )
    except Exception as e:
        raise RuntimeError(f"Parakeet transcription failed: {e}") from e

    elapsed = time.time() - t0

    # parakeet-mlx returns a dict with 'text' and 'segments'
    # segments: list of dicts with 'start', 'end', 'text'
    text = result.get("text", "").strip()
    segments_raw = result.get("segments", [])

    if not text and not segments_raw:
        raise RuntimeError("Parakeet produced no output")

    # Build timestamped text from segments
    segments: list[tuple[str, str]] = []
    last_end_seconds = 0

    for seg in segments_raw:
        start_secs = seg.get("start", 0)
        end_secs = seg.get("end", 0)
        seg_text = seg.get("text", "").strip()
        if not seg_text:
            continue

        h = int(start_secs // 3600)
        m = int((start_secs % 3600) // 60)
        s = int(start_secs % 60)
        timestamp = f"[{h:02d}:{m:02d}:{s:02d}]"
        segments.append((timestamp, seg_text))
        last_end_seconds = max(last_end_seconds, end_secs)

    duration_minutes = max(1, round(last_end_seconds / 60))

    # Apply the same transcript filter used for whisper output
    if segments:
        from app.transcript_filter import filter_segments
        segments = filter_segments(segments)
        timestamped_lines = [f"{ts} {text}" for ts, text in segments]
        timestamped_text = "\n".join(timestamped_lines)
        plain_text = " ".join(text for _, text in segments)
    else:
        timestamped_text = text
        plain_text = text

    logger.info(
        "Parakeet transcription complete: %d segments, ~%d min, %.1fs wall time",
        len(segments),
        duration_minutes,
        elapsed,
    )

    return TranscriptionResult(
        plain_text=plain_text,
        timestamped_text=timestamped_text,
        duration_minutes=duration_minutes,
        srt_path="",  # Parakeet doesn't produce SRT files
    )
