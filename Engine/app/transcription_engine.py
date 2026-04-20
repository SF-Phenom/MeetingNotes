"""Transcription-engine protocols + factory.

Thin abstraction layer over the concrete transcription backends so the
pipeline (batch) and TranscriptionManager (realtime) don't import Parakeet
symbols directly. Adding a second engine — WhisperKit is the one flagged
on the roadmap — should be a two-step change:

  1. Write a new class (or module) that implements the protocol below.
  2. Add a branch to the factory below.

Nothing else in the app should need to change.

Structural (duck-typed) protocols. No inheritance required on the
concrete classes — a class satisfies the protocol by shape, which means
the existing ``RealtimeTranscriber`` in realtime_transcriber.py already
counts as a ``RealtimeEngine`` without being edited.
"""
from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

from app.transcriber import TranscriptionResult

logger = logging.getLogger(__name__)


# Environment override for picking a non-default engine — wins over the
# user's persisted preference (e.g. for an A/B comparison or a WhisperKit
# spike). If unset, the value from state.json's ``transcription_engine``
# field is used; that field defaults to "parakeet".
ENGINE_ENV_VAR = "MEETINGNOTES_TRANSCRIPTION_ENGINE"
DEFAULT_ENGINE = "parakeet"
SUPPORTED_ENGINES = ("parakeet", "apple_speech")


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class BatchEngine(Protocol):
    """Offline, all-at-once transcription of a WAV file on disk."""

    def transcribe(
        self, wav_path: str, *, hints: dict | None = None,
    ) -> TranscriptionResult:
        """Return the TranscriptionResult for ``wav_path``.

        ``hints`` is an optional dict of pipeline-layer context the engine
        may consult but is always free to ignore. Currently the Parakeet
        engine reads ``hints["participant_count"]`` to route diarization
        between FluidAudio's community-1 and Sortformer models. Unknown
        keys are silently ignored.

        Should raise ``RuntimeError`` on any engine-specific failure so the
        pipeline can record the transcript as "Transcription unavailable"
        without the wav being lost.
        """
        ...


@runtime_checkable
class RealtimeEngine(Protocol):
    """Incremental transcription of a WAV file that grows on disk during
    recording. Implementations poll the file and publish partial text.
    """

    def start(self, wav_path: str) -> None:
        """Begin polling ``wav_path`` on a background thread."""
        ...

    def stop(self) -> str:
        """Stop polling and return the accumulated text (possibly empty)."""
        ...

    @property
    def is_busy(self) -> bool:
        """True if a transcription cycle is currently running on the GPU."""
        ...

    @property
    def live_transcript_path(self) -> str | None:
        """Path to a .live.txt file that updates during recording, or None."""
        ...


# ---------------------------------------------------------------------------
# Parakeet adapters
# ---------------------------------------------------------------------------


class ParakeetBatchEngine:
    """Adapter — wraps the module-level transcribe_with_parakeet function."""

    def transcribe(
        self, wav_path: str, *, hints: dict | None = None,
    ) -> TranscriptionResult:
        # Late import so the heavy parakeet-mlx module is only loaded when
        # we're actually about to transcribe — saves app-startup time.
        from app.transcriber import transcribe_with_parakeet
        return transcribe_with_parakeet(wav_path, hints=hints)


# ---------------------------------------------------------------------------
# Apple Speech adapters
# ---------------------------------------------------------------------------


class AppleSpeechBatchEngine:
    """Adapter — wraps speech_transcriber.transcribe_file (NDJSON IPC).

    Apple Speech returns plain text only, so timestamped_text mirrors
    plain_text and srt_path is empty (same shape Parakeet uses when SRT
    isn't generated).
    """

    def transcribe(
        self, wav_path: str, *, hints: dict | None = None,
    ) -> TranscriptionResult:
        # Apple Speech has no diarization path and no other hint consumers,
        # so ``hints`` is intentionally ignored.
        del hints
        from app.speech_transcriber import transcribe_file
        text = transcribe_file(wav_path)
        if not text:
            raise RuntimeError(
                "Apple Speech returned no text — check that Dictation is "
                "enabled in System Settings and the on-device model is "
                "downloaded."
            )
        return TranscriptionResult(
            plain_text=text,
            timestamped_text=text,
            duration_minutes=0,
            srt_path="",
        )


# ---------------------------------------------------------------------------
# Availability + factory
# ---------------------------------------------------------------------------


def is_engine_available(name: str) -> bool:
    """Return True if the named engine can run on this machine.

    Parakeet is treated as always-available (it's a pip dependency,
    failure to import surfaces at transcribe time). Apple Speech requires
    the SpeechTranscribe.app bundle that setup builds — gate the menubar
    UI on this.
    """
    name = name.strip().lower()
    if name == "parakeet":
        return True
    if name == "apple_speech":
        from app.speech_transcriber import is_available
        return is_available()
    return False


def _resolve_engine_name() -> str:
    """Pick an engine: env var override > state.json preference > default."""
    env = os.environ.get(ENGINE_ENV_VAR, "").strip().lower()
    if env:
        return env
    # Late import — keeps the module importable from contexts where state
    # isn't on disk yet (notably tests that don't write a state.json).
    try:
        from app.state import load as load_state
        return str(load_state().get("transcription_engine", DEFAULT_ENGINE)).strip().lower()
    except Exception as e:  # noqa: BLE001 — engine selection must never crash startup
        logger.warning("Could not read engine preference from state (%s); using default.", e)
        return DEFAULT_ENGINE


def _unknown_engine(name: str) -> "ValueError":
    return ValueError(
        "Unknown transcription engine: {!r}. Supported: {}. "
        "Set the {} env var to override.".format(
            name, ", ".join(SUPPORTED_ENGINES), ENGINE_ENV_VAR,
        )
    )


def get_batch_engine() -> BatchEngine:
    """Return the batch transcription engine selected by env or state."""
    name = _resolve_engine_name()
    if name == "parakeet":
        return ParakeetBatchEngine()
    if name == "apple_speech":
        return AppleSpeechBatchEngine()
    raise _unknown_engine(name)


def get_realtime_engine() -> RealtimeEngine:
    """Return a new realtime transcription engine instance."""
    name = _resolve_engine_name()
    if name == "parakeet":
        # Late import for the same reason as the batch adapter.
        from app.realtime_transcriber import RealtimeTranscriber
        return RealtimeTranscriber()
    if name == "apple_speech":
        from app.speech_transcriber import SpeechRealtimeTranscriber
        return SpeechRealtimeTranscriber()
    raise _unknown_engine(name)
