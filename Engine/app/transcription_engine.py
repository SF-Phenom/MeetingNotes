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


# Environment override used to pick a non-default engine (e.g. for an
# A/B comparison or a WhisperKit spike). If unset or set to "parakeet"
# the current Parakeet-via-MLX backends are used.
ENGINE_ENV_VAR = "MEETINGNOTES_TRANSCRIPTION_ENGINE"
DEFAULT_ENGINE = "parakeet"


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class BatchEngine(Protocol):
    """Offline, all-at-once transcription of a WAV file on disk."""

    def transcribe(self, wav_path: str) -> TranscriptionResult:
        """Return the TranscriptionResult for ``wav_path``.

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

    def transcribe(self, wav_path: str) -> TranscriptionResult:
        # Late import so the heavy parakeet-mlx module is only loaded when
        # we're actually about to transcribe — saves app-startup time.
        from app.transcriber import transcribe_with_parakeet
        return transcribe_with_parakeet(wav_path)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _resolve_engine_name() -> str:
    """Read and normalize the engine name from the environment."""
    return os.environ.get(ENGINE_ENV_VAR, DEFAULT_ENGINE).strip().lower()


def _unknown_engine(name: str) -> "ValueError":
    return ValueError(
        "Unknown transcription engine: {!r}. Supported: parakeet. "
        "Set the {} env var to override.".format(name, ENGINE_ENV_VAR)
    )


def get_batch_engine() -> BatchEngine:
    """Return the batch transcription engine selected by the environment."""
    name = _resolve_engine_name()
    if name == "parakeet":
        return ParakeetBatchEngine()
    raise _unknown_engine(name)


def get_realtime_engine() -> RealtimeEngine:
    """Return a new realtime transcription engine instance."""
    name = _resolve_engine_name()
    if name == "parakeet":
        # Late import for the same reason as the batch adapter.
        from app.realtime_transcriber import RealtimeTranscriber
        return RealtimeTranscriber()
    raise _unknown_engine(name)
