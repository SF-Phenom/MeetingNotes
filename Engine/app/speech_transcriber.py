"""
Apple Speech transcriber — uses macOS SFSpeechRecognizer via a Swift helper
binary to provide on-device speech-to-text as an alternative to Parakeet.

The Swift binary lives inside an app bundle at .bin/SpeechTranscribe.app
and must be launched via `open` so macOS TCC grants the Speech Recognition
and Microphone permissions declared in its Info.plist.

Communication is via an NDJSON output file:
  {"type": "log",     "text": "..."}   — diagnostic messages
  {"type": "partial", "text": "..."}   — intermediate transcription
  {"type": "final",   "text": "..."}   — completed transcription
  {"type": "error",   "text": "..."}   — error message

Two modes:
  - File mode: transcribe a completed WAV file
  - Watch mode: transcribe a growing WAV during recording (live)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import threading
import time

logger = logging.getLogger(__name__)

# Path to the app bundle (relative to Engine/)
_ENGINE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_BUNDLE = os.path.join(_ENGINE_DIR, ".bin", "SpeechTranscribe.app")
BINARY_PATH = os.path.join(APP_BUNDLE, "Contents", "MacOS", "speech-transcribe")

# How often to check the output file for new lines
_POLL_INTERVAL = 1.0


def is_available() -> bool:
    """Check if the SpeechTranscribe app bundle exists and is executable."""
    return os.path.isfile(BINARY_PATH) and os.access(BINARY_PATH, os.X_OK)


def _launch(wav_path: str, output_path: str, watch: bool = False,
            locale: str = "en_US") -> subprocess.Popen | None:
    """Launch the SpeechTranscribe app bundle via `open`.

    Returns the Popen for the `open` command (which exits immediately;
    the actual speech-transcribe process runs independently).
    """
    if not is_available():
        logger.error("SpeechTranscribe app bundle not found at %s", APP_BUNDLE)
        return None

    cmd = [
        "open", APP_BUNDLE,
        "--args",
        "--input", os.path.abspath(wav_path),
        "--output", os.path.abspath(output_path),
        "--locale", locale,
    ]
    if watch:
        cmd.append("--watch")

    logger.info("Launching SpeechTranscribe: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        proc.wait()  # `open` returns immediately
        return proc
    except Exception as e:
        logger.error("Failed to launch SpeechTranscribe: %s", e)
        return None


def _read_ndjson(output_path: str, timeout: float = 300.0,
                 on_partial: callable | None = None) -> str | None:
    """Read the NDJSON output file until a final or error line appears.

    Args:
        output_path: Path to the NDJSON file.
        timeout: Max seconds to wait for completion.
        on_partial: Optional callback for partial results.

    Returns:
        The final transcription text, or None on error/timeout.
    """
    start = time.monotonic()
    last_pos = 0
    final_text = None

    while time.monotonic() - start < timeout:
        if not os.path.exists(output_path):
            time.sleep(_POLL_INTERVAL)
            continue

        try:
            with open(output_path, "r", encoding="utf-8") as f:
                f.seek(last_pos)
                new_data = f.read()
                if new_data:
                    last_pos += len(new_data)
                    for line in new_data.strip().split("\n"):
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("type", "")
                        text = msg.get("text", "")

                        if msg_type == "final":
                            final_text = text
                            return final_text
                        elif msg_type == "partial":
                            if on_partial:
                                on_partial(text)
                        elif msg_type == "error":
                            logger.error("SpeechTranscribe error: %s", text)
                            return None
                        elif msg_type == "log":
                            logger.debug("SpeechTranscribe: %s", text)
        except OSError:
            pass

        time.sleep(_POLL_INTERVAL)

    logger.warning("SpeechTranscribe timed out after %.0fs", timeout)
    return None


def transcribe_file(wav_path: str, locale: str = "en_US",
                    timeout: float = 300.0) -> str | None:
    """Transcribe a completed WAV file using Apple Speech.

    Args:
        wav_path: Path to a WAV file.
        locale: BCP 47 locale (default en_US).
        timeout: Max seconds to wait.

    Returns:
        Transcription text, or None on failure.
    """
    from app.corrections import apply_corrections

    with tempfile.NamedTemporaryFile(suffix=".ndjson", delete=False,
                                     prefix="speech_out_") as tmp:
        output_path = tmp.name

    try:
        proc = _launch(wav_path, output_path, watch=False, locale=locale)
        if proc is None:
            return None

        result = _read_ndjson(output_path, timeout=timeout)
        if result:
            result = apply_corrections(result)
        return result
    finally:
        try:
            os.unlink(output_path)
        except OSError:
            pass


class SpeechRealtimeTranscriber:
    """Reads a growing WAV file and produces live transcripts via Apple Speech.

    Follows the same interface as RealtimeTranscriber but delegates to
    the SpeechTranscribe Swift binary in --watch mode.
    """

    def __init__(self):
        self._wav_path: str | None = None
        self._live_txt_path: str | None = None
        self._output_path: str | None = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._current_text: str = ""

    @property
    def live_transcript_path(self) -> str | None:
        """Path to the .live.txt file that updates during recording."""
        return self._live_txt_path

    @property
    def is_busy(self) -> bool:
        """True while the transcriber is running."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def accumulated_sentences(self) -> list:
        """Apple Speech doesn't produce per-sentence timestamps, so speaker
        diarization isn't wired for this engine. Satisfies the
        :class:`RealtimeEngine` protocol by returning an empty list —
        pipeline-level diarization treats that as "skip diarization."
        """
        return []

    def start(self, wav_path: str) -> None:
        """Begin transcribing the growing WAV file."""
        if self._thread and self._thread.is_alive():
            logger.warning("SpeechRealtimeTranscriber already running")
            return

        self._wav_path = wav_path
        self._current_text = ""
        self._stop_event.clear()

        # Live transcript file
        base = os.path.splitext(wav_path)[0]
        self._live_txt_path = base + ".live.txt"
        with open(self._live_txt_path, "w", encoding="utf-8") as f:
            f.write("[Live transcription starting...]\n")

        # NDJSON output file
        self._output_path = tempfile.mktemp(suffix=".ndjson",
                                            prefix="speech_live_")

        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Apple Speech transcriber started for %s",
                     os.path.basename(wav_path))

    def stop(self) -> str:
        """Stop transcription and return accumulated text."""
        self._stop_event.set()

        # Send SIGTERM to the speech-transcribe process to trigger finalization
        self._signal_stop()

        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

        # Clean up
        if self._live_txt_path and os.path.exists(self._live_txt_path):
            try:
                os.remove(self._live_txt_path)
            except OSError:
                pass
        if self._output_path and os.path.exists(self._output_path):
            try:
                os.unlink(self._output_path)
            except OSError:
                pass

        from app.corrections import apply_corrections
        result = apply_corrections(self._current_text) if self._current_text else ""
        logger.info("Apple Speech transcriber stopped. %d chars", len(result))
        return result

    def _signal_stop(self) -> None:
        """Send SIGTERM to the running speech-transcribe process."""
        import signal
        try:
            # Find the process by checking for our output file in args
            result = subprocess.run(
                ["pgrep", "-f", "speech-transcribe.*--output"],
                capture_output=True, text=True, timeout=5,
            )
            for pid_str in result.stdout.strip().split("\n"):
                if pid_str.strip():
                    pid = int(pid_str.strip())
                    os.kill(pid, signal.SIGTERM)
                    logger.debug("Sent SIGTERM to speech-transcribe pid %d", pid)
        except Exception as e:
            logger.debug("Could not signal speech-transcribe: %s", e)

    def _run(self) -> None:
        """Background thread: launch Swift binary and read output."""
        proc = _launch(self._wav_path, self._output_path, watch=True)
        if proc is None:
            return

        # Poll the output file for updates
        last_pos = 0
        while not self._stop_event.is_set():
            self._stop_event.wait(timeout=_POLL_INTERVAL)

            if not self._output_path or not os.path.exists(self._output_path):
                continue

            try:
                with open(self._output_path, "r", encoding="utf-8") as f:
                    f.seek(last_pos)
                    new_data = f.read()
                    if not new_data:
                        continue
                    last_pos += len(new_data)

                    for line in new_data.strip().split("\n"):
                        if not line:
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("type", "")
                        text = msg.get("text", "")

                        if msg_type in ("partial", "final"):
                            self._current_text = text
                            self._update_live_file()
                        elif msg_type == "error":
                            logger.error("SpeechTranscribe: %s", text)
                        elif msg_type == "log":
                            logger.debug("SpeechTranscribe: %s", text)
            except OSError:
                pass

    def _update_live_file(self) -> None:
        """Write current transcript to the .live.txt file."""
        if not self._live_txt_path or not self._current_text:
            return
        try:
            with open(self._live_txt_path, "w", encoding="utf-8") as f:
                f.write(self._current_text)
        except OSError as e:
            logger.debug("Could not update live file: %s", e)
