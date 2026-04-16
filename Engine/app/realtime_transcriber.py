"""
Realtime transcriber — reads a growing WAV file during recording and
produces a live transcript using parakeet-mlx.

The Swift audio capture binary writes PCM data to a WAV file continuously.
This module polls that file, reads new audio bytes, and periodically
transcribes accumulated audio to produce partial transcripts.

Audio is divided into fixed-size chunks (MAX_CHUNK_SECS each).  Each chunk
is re-transcribed as it grows, and when a chunk boundary is crossed, its
text is finalized and the next chunk begins.  This keeps GPU memory and
inference time bounded regardless of recording length.

Usage:
    rt = RealtimeTranscriber()
    rt.start(wav_path)        # begins background polling + transcription
    ...                       # recording happens
    transcript = rt.stop()    # returns final accumulated transcript
"""

from __future__ import annotations

import logging
import math
import os
import struct
import tempfile
import threading
import time
import wave

logger = logging.getLogger(__name__)

# How often to check for new audio and transcribe (seconds)
POLL_INTERVAL = 15

# Minimum new audio (seconds) before triggering a transcription
MIN_NEW_AUDIO_SECS = 5

# Fixed chunk size (seconds).  Each chunk is transcribed independently;
# completed chunks are finalized as text and never re-processed.
MAX_CHUNK_SECS = 300

# WAV format constants (must match Swift capture binary output)
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1
WAV_HEADER_SIZE = 44
BYTES_PER_SEC = SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS

# Audio below this RMS dBFS threshold is effectively silence.
# Normal speech is -20 to -6 dBFS; -50 dBFS is extremely quiet.
SILENCE_THRESHOLD_DBFS = -50



class RealtimeTranscriber:
    """Reads a growing WAV file and produces live partial transcripts."""

    def __init__(self):
        self._wav_path: str | None = None
        self._live_txt_path: str | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # Set by the GPU watchdog when mx.synchronize blocks longer than its
        # timeout. The main loop checks this before each cycle and aborts
        # gracefully instead of continuing to issue GPU work on a wedged stream.
        self._unhealthy_event = threading.Event()
        self._model = None
        self._stream = None  # Reusable MLX GPU stream
        self._transcribing: bool = False  # Guard against overlapping cycles

        # Chunk tracking
        self._chunk_index: int = 0  # Which fixed chunk we're transcribing
        self._chunk_texts: list[str] = []  # Finalized text per completed chunk
        self._current_chunk_text: str = ""  # Latest text for the in-progress chunk
        self._last_file_size: int = WAV_HEADER_SIZE  # Last observed file size

    @property
    def live_transcript_path(self) -> str | None:
        """Path to the .live.txt file that updates during recording."""
        return self._live_txt_path

    @property
    def is_busy(self) -> bool:
        """True if a transcription cycle is currently running on the GPU."""
        return self._transcribing

    def start(self, wav_path: str) -> None:
        """Begin polling the WAV file and transcribing in the background."""
        if self._thread and self._thread.is_alive():
            logger.warning("RealtimeTranscriber already running")
            return

        self._wav_path = wav_path
        self._chunk_index = 0
        self._chunk_texts = []
        self._current_chunk_text = ""
        self._last_file_size = WAV_HEADER_SIZE
        self._stop_event.clear()
        self._unhealthy_event.clear()

        # Live transcript file: same name as WAV but .live.txt
        base = os.path.splitext(wav_path)[0]
        self._live_txt_path = base + ".live.txt"

        # Write initial empty file
        with open(self._live_txt_path, "w", encoding="utf-8") as f:
            f.write("[Live transcription starting...]\n")

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Realtime transcriber started for %s", os.path.basename(wav_path))

    def stop(self) -> str:
        """Stop polling and return the accumulated transcript immediately.

        Returns whatever text the background thread has produced so far
        without blocking the main thread for a final transcription pass.
        """
        self._stop_event.set()

        # Give the background thread a brief moment to finish if it's
        # between iterations, but don't block the UI waiting for a long
        # transcription pass to complete.
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

        # Clean up live transcript file
        if self._live_txt_path and os.path.exists(self._live_txt_path):
            try:
                os.remove(self._live_txt_path)
            except OSError:
                pass

        # Release model and GPU stream
        self._stream = None
        self._model = None

        accumulated = self._full_text()
        logger.info(
            "Realtime transcriber stopped. Accumulated %d chars",
            len(accumulated),
        )
        return accumulated

    def _full_text(self) -> str:
        """Combine finalized chunk texts with the current in-progress chunk."""
        from app.corrections import apply_corrections
        parts = [t for t in self._chunk_texts if t]
        if self._current_chunk_text:
            parts.append(self._current_chunk_text)
        return apply_corrections(" ".join(parts))

    def _run(self) -> None:
        """Background loop: poll for new audio, transcribe periodically."""
        # Load model upfront so first transcription is fast
        try:
            self._load_model()
        except Exception as e:
            logger.error("Failed to load Parakeet model for realtime: %s", e)
            return

        while not self._stop_event.is_set():
            # If the GPU watchdog marked this session unhealthy, stop issuing
            # new MLX work. The finalize path (stop()) still returns whatever
            # chunks were completed before the wedge.
            if self._unhealthy_event.is_set():
                logger.error(
                    "Realtime session marked unhealthy — aborting poll loop."
                )
                break
            self._stop_event.wait(timeout=POLL_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._transcribe_new_audio()
            except Exception as e:
                logger.error("Realtime transcription error: %s", e, exc_info=True)

    def _load_model(self) -> None:
        """Load the parakeet-mlx model and create a reusable GPU stream."""
        if self._model is not None:
            return
        import mlx.core as mx
        from parakeet_mlx import from_pretrained
        from app.transcriber import PARAKEET_MODEL_ID, MLX_MEMORY_LIMIT
        # Cap GPU memory so we can't starve the system
        mx.metal.set_memory_limit(MLX_MEMORY_LIMIT)
        logger.info("Loading Parakeet model for realtime transcription...")
        self._model = from_pretrained(PARAKEET_MODEL_ID)
        self._stream = mx.new_stream(mx.gpu)
        logger.info("Parakeet model loaded for realtime")

    def _transcribe_new_audio(self) -> None:
        """Read new bytes from the WAV file and transcribe if enough has accumulated."""
        if self._transcribing:
            logger.warning("Previous transcription cycle still running, skipping")
            return
        if not self._wav_path or not os.path.exists(self._wav_path):
            return

        self._transcribing = True
        try:
            self._do_transcribe()
        finally:
            self._transcribing = False

    def _do_transcribe(self) -> None:
        """Inner transcription logic using fixed chunks."""
        file_size = os.path.getsize(self._wav_path)
        total_pcm = file_size - WAV_HEADER_SIZE

        if file_size <= self._last_file_size:
            return

        # Check if enough new audio has arrived to bother transcribing
        new_bytes = file_size - self._last_file_size
        new_secs = new_bytes / BYTES_PER_SEC
        if new_secs < MIN_NEW_AUDIO_SECS:
            return

        chunk_bytes = MAX_CHUNK_SECS * BYTES_PER_SEC

        # Check if the recording has crossed into a new chunk.
        # Advance based on byte position — even if Parakeet produced no text
        # for the current chunk, we must still move forward.
        current_chunk_end_byte = (self._chunk_index + 1) * chunk_bytes
        while total_pcm > current_chunk_end_byte:
            self._chunk_texts.append(self._current_chunk_text)
            logger.info(
                "Chunk %d finalized (%d chars). Starting chunk %d.",
                self._chunk_index,
                len(self._current_chunk_text),
                self._chunk_index + 1,
            )
            self._current_chunk_text = ""
            self._chunk_index += 1
            current_chunk_end_byte = (self._chunk_index + 1) * chunk_bytes

        # Determine byte range for the current chunk
        chunk_start_byte = self._chunk_index * chunk_bytes
        chunk_pcm_available = total_pcm - chunk_start_byte

        if chunk_pcm_available <= 0:
            return

        # Read only the current chunk's audio
        read_start = WAV_HEADER_SIZE + chunk_start_byte
        read_end = WAV_HEADER_SIZE + min(
            chunk_start_byte + chunk_bytes, total_pcm
        )

        try:
            with open(self._wav_path, "rb") as f:
                f.seek(read_start)
                pcm_data = f.read(read_end - read_start)
        except OSError as e:
            logger.warning("Could not read WAV file: %s", e)
            return

        self._last_file_size = file_size

        if not pcm_data:
            return

        # Check audio level — warn early if recording is near-silence
        try:
            n_samples = len(pcm_data) // SAMPLE_WIDTH
            if n_samples > 0:
                samples = struct.unpack(f"<{n_samples}h", pcm_data[:n_samples * SAMPLE_WIDTH])
                rms = math.sqrt(sum(s * s for s in samples) / n_samples)
                db_rms = 20 * math.log10(rms / 32768) if rms > 0 else -100
                if db_rms < SILENCE_THRESHOLD_DBFS:
                    logger.warning(
                        "Audio level very low (%.1f dBFS) — mic may not be "
                        "capturing speech. Check that the correct input device "
                        "is selected.",
                        db_rms,
                    )
        except Exception:
            pass  # Audio check is best-effort, don't block transcription

        # Write PCM data to a temporary WAV file for Parakeet
        tmp_wav = None
        try:
            tmp_wav = tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, dir=os.path.dirname(self._wav_path)
            )
            with wave.open(tmp_wav.name, "wb") as wf:
                wf.setnchannels(CHANNELS)
                wf.setsampwidth(SAMPLE_WIDTH)
                wf.setframerate(SAMPLE_RATE)
                wf.writeframes(pcm_data)

            # Transcribe the current chunk.
            import mlx.core as mx
            from parakeet_mlx.parakeet import DecodingConfig, Beam
            from app.transcriber import PARAKEET_BEAM_SIZE, synchronize_with_watchdog
            decoding = DecodingConfig(decoding=Beam(beam_size=PARAKEET_BEAM_SIZE))
            t0 = time.time()
            with mx.stream(self._stream):
                result = self._model.transcribe(tmp_wav.name, decoding_config=decoding)
            # Watchdog: if synchronize blocks longer than GPU_SYNC_WATCHDOG_SECS
            # the session is marked unhealthy so the next loop iteration aborts.
            synchronize_with_watchdog(
                self._stream,
                name="parakeet-realtime",
                on_timeout=self._unhealthy_event.set,
            )
            mx.metal.clear_cache()
            elapsed = time.time() - t0

            text = result.text.strip()
            chunk_secs = len(pcm_data) / BYTES_PER_SEC

            logger.info(
                "Realtime transcription: chunk %d, %.0fs audio in %.1fs (%.1fx realtime)",
                self._chunk_index, chunk_secs, elapsed,
                chunk_secs / max(elapsed, 0.01),
            )

            if text:
                self._current_chunk_text = text
                self._write_live_transcript(self._full_text())

        except Exception as e:
            logger.error("Realtime Parakeet transcription failed: %s", e)
        finally:
            if tmp_wav and os.path.exists(tmp_wav.name):
                try:
                    os.unlink(tmp_wav.name)
                except OSError:
                    pass
            # Release pcm buffer
            pcm_data = None  # noqa: F841

    def _write_live_transcript(self, text: str) -> None:
        """Update the .live.txt file with the current transcript."""
        if not self._live_txt_path:
            return
        try:
            with open(self._live_txt_path, "w", encoding="utf-8") as f:
                f.write(text)
                f.write("\n")
        except OSError as e:
            logger.warning("Could not write live transcript: %s", e)
