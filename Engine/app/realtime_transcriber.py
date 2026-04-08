"""
Realtime transcriber — reads a growing WAV file during recording and
produces a live transcript using parakeet-mlx.

The Swift audio capture binary writes PCM data to a WAV file continuously.
This module polls that file, reads new audio bytes, and periodically
transcribes accumulated audio to produce partial transcripts.

Usage:
    rt = RealtimeTranscriber()
    rt.start(wav_path)        # begins background polling + transcription
    ...                       # recording happens
    transcript = rt.stop()    # returns final accumulated transcript
"""

from __future__ import annotations

import logging
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

# WAV format constants (must match Swift capture binary output)
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16-bit
CHANNELS = 1
WAV_HEADER_SIZE = 44


class RealtimeTranscriber:
    """Reads a growing WAV file and produces live partial transcripts."""

    def __init__(self):
        self._wav_path: str | None = None
        self._live_txt_path: str | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._accumulated_text: str = ""
        self._bytes_read: int = WAV_HEADER_SIZE  # skip WAV header
        self._model = None

    @property
    def live_transcript_path(self) -> str | None:
        """Path to the .live.txt file that updates during recording."""
        return self._live_txt_path

    def start(self, wav_path: str) -> None:
        """Begin polling the WAV file and transcribing in the background."""
        if self._thread and self._thread.is_alive():
            logger.warning("RealtimeTranscriber already running")
            return

        self._wav_path = wav_path
        self._bytes_read = WAV_HEADER_SIZE
        self._accumulated_text = ""
        self._stop_event.clear()

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
        """Stop polling and return the final accumulated transcript."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=30)
            self._thread = None

        # Do one final transcription of any remaining audio
        self._transcribe_new_audio(final=True)

        # Clean up live transcript file
        if self._live_txt_path and os.path.exists(self._live_txt_path):
            try:
                os.remove(self._live_txt_path)
            except OSError:
                pass

        # Release model memory
        self._model = None

        logger.info(
            "Realtime transcriber stopped. Accumulated %d chars",
            len(self._accumulated_text),
        )
        return self._accumulated_text

    def _run(self) -> None:
        """Background loop: poll for new audio, transcribe periodically."""
        # Load model upfront so first transcription is fast
        try:
            self._load_model()
        except Exception as e:
            logger.error("Failed to load Parakeet model for realtime: %s", e)
            return

        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=POLL_INTERVAL)
            if self._stop_event.is_set():
                break
            try:
                self._transcribe_new_audio()
            except Exception as e:
                logger.error("Realtime transcription error: %s", e, exc_info=True)

    def _load_model(self) -> None:
        """Load the parakeet-mlx model."""
        if self._model is not None:
            return
        logger.info("Loading Parakeet model for realtime transcription...")
        from parakeet_mlx import from_pretrained
        from app.transcriber import PARAKEET_MODEL_ID
        self._model = from_pretrained(PARAKEET_MODEL_ID)
        logger.info("Parakeet model loaded for realtime")

    def _transcribe_new_audio(self, final: bool = False) -> None:
        """Read new bytes from the WAV file and transcribe if enough has accumulated."""
        if not self._wav_path or not os.path.exists(self._wav_path):
            return

        file_size = os.path.getsize(self._wav_path)
        new_bytes_available = file_size - self._bytes_read

        if new_bytes_available <= 0:
            return

        # Check if we have enough new audio to bother transcribing
        new_secs = new_bytes_available / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)
        if not final and new_secs < MIN_NEW_AUDIO_SECS:
            return

        # Read all audio from the beginning (after header) for full context
        # This gives Parakeet the complete audio, producing a coherent transcript
        try:
            with open(self._wav_path, "rb") as f:
                f.seek(WAV_HEADER_SIZE)
                pcm_data = f.read(file_size - WAV_HEADER_SIZE)
        except OSError as e:
            logger.warning("Could not read WAV file: %s", e)
            return

        self._bytes_read = file_size

        if not pcm_data:
            return

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

            # Transcribe the complete audio so far.
            # Use a dedicated GPU stream to avoid Metal command buffer
            # collisions with the main thread's AppKit run loop.
            import mlx.core as mx
            from parakeet_mlx.parakeet import DecodingConfig, Beam
            from app.transcriber import PARAKEET_BEAM_SIZE
            decoding = DecodingConfig(decoding=Beam(beam_size=PARAKEET_BEAM_SIZE))
            stream = mx.new_stream(mx.gpu)
            t0 = time.time()
            with mx.stream(stream):
                result = self._model.transcribe(tmp_wav.name, decoding_config=decoding)
            mx.synchronize(stream)
            elapsed = time.time() - t0

            text = result.text.strip()
            total_secs = len(pcm_data) / (SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)

            logger.info(
                "Realtime transcription: %.0fs audio in %.1fs (%.1fx realtime)",
                total_secs, elapsed, total_secs / max(elapsed, 0.01),
            )

            if text:
                self._accumulated_text = text
                self._write_live_transcript(text)

        except Exception as e:
            logger.error("Realtime Parakeet transcription failed: %s", e)
        finally:
            if tmp_wav and os.path.exists(tmp_wav.name):
                try:
                    os.unlink(tmp_wav.name)
                except OSError:
                    pass

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
