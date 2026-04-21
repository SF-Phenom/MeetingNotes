"""Diarizer Protocol + factory for MeetingNotes.

A *diarizer* takes a WAV path and returns a list of
:class:`~app.speaker_alignment.SpeakerSegment` records — time ranges
labelled with per-recording speaker identities (``Speaker A``, ``Speaker B``,
…). Speaker labels do not carry over between recordings.

The real backend (FluidAudio CoreML CLI, shelled out to a Swift binary)
lands in the next commit. This module currently ships:

* :class:`Diarizer` — the structural Protocol every backend must satisfy.
* :class:`FakeDiarizer` — a deterministic test double that alternates
  speakers on a fixed cadence. Used by the unit tests and triggerable
  at runtime via ``MEETINGNOTES_DIARIZER=fake`` for plumbing checks on
  real WAV files before the real backend exists.
* :func:`get_diarizer` — factory that returns ``None`` by default (so
  the pipeline no-ops when no backend is wired), and returns a
  :class:`FakeDiarizer` when the env var above is set.

The pipeline also gates on ``state.diarization_enabled`` — ``None`` from
this factory and ``False`` on the state flag are both "don't diarize",
evaluated independently.
"""
from __future__ import annotations

import logging
import os
import wave
from typing import Protocol, runtime_checkable

from app.speaker_alignment import SpeakerSegment

logger = logging.getLogger(__name__)


# Environment override for selecting a diarizer backend. Unset ⇒ no
# backend (None returned). The only currently-supported value is "fake"
# — real backends land alongside their implementation.
DIARIZER_ENV_VAR = "MEETINGNOTES_DIARIZER"


@runtime_checkable
class Diarizer(Protocol):
    """Offline speaker diarization of a WAV file on disk.

    Implementations are expected to be robust to empty / very-short /
    single-speaker audio and should return ``[]`` rather than raising
    in those cases. Returning ``None`` signals an unrecoverable failure
    the pipeline should treat as "no diarization this time" without
    failing the whole transcription.

    ``model`` is an optional backend-specific hint (e.g. ``"community-1"``
    or ``"sortformer"`` for the FluidAudio backend). Implementations that
    don't understand the value — including the test double — silently
    ignore it.

    ``min_speakers`` / ``max_speakers`` are optional bounds hints for the
    underlying clusterer. Callers pass them when they have external
    knowledge of how many people are in the meeting (calendar attendees,
    observed participant count). Backends that can't use them — including
    sortformer (architectural 4-track cap) and the test double — ignore
    them silently.
    """

    def diarize(
        self,
        wav_path: str,
        model: str | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment] | None: ...


class FakeDiarizer:
    """Alternates speakers on a fixed cadence over the audio's duration.

    Not a real diarizer — it does not look at the audio content. Its only
    purpose is to exercise the plumbing: reading the WAV header for
    duration, handing back valid ``SpeakerSegment``s that the alignment
    layer and formatter can consume. Useful both for unit tests (where
    real backends would be overkill) and for end-to-end smoke tests on
    real recordings before the Swift CLI backend is available.
    """

    def __init__(
        self,
        period_secs: float = 15.0,
        speakers: tuple[str, ...] = ("Speaker A", "Speaker B"),
    ) -> None:
        if period_secs <= 0:
            raise ValueError("period_secs must be positive")
        if not speakers:
            raise ValueError("speakers must be non-empty")
        self._period = period_secs
        self._speakers = speakers

    def diarize(
        self,
        wav_path: str,
        model: str | None = None,
        min_speakers: int | None = None,
        max_speakers: int | None = None,
    ) -> list[SpeakerSegment]:
        # ``model`` / ``min_speakers`` / ``max_speakers`` are accepted to
        # conform to the Diarizer Protocol but have no meaning for the fake —
        # we always produce the same alternating output regardless.
        del model, min_speakers, max_speakers
        try:
            with wave.open(wav_path, "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
        except (wave.Error, OSError) as e:
            logger.warning("FakeDiarizer: could not read %s (%s)", wav_path, e)
            return []
        if rate <= 0 or frames <= 0:
            return []
        duration = frames / rate

        segments: list[SpeakerSegment] = []
        start = 0.0
        i = 0
        while start < duration:
            end = min(start + self._period, duration)
            segments.append(
                SpeakerSegment(
                    start=start,
                    end=end,
                    speaker=self._speakers[i % len(self._speakers)],
                )
            )
            start = end
            i += 1
        return segments


def get_diarizer() -> Diarizer | None:
    """Return a diarizer instance, or ``None`` when no backend is configured.

    Order of precedence:

    1. Explicit ``MEETINGNOTES_DIARIZER`` env var (``fake`` or
       ``fluidaudio``) selects that backend.
    2. Otherwise — if the FluidAudio Swift binary exists on disk — use it.
    3. Otherwise ``None``. The caller treats this as "no diarization this
       time" without failing the pipeline.

    The caller is responsible for also checking
    ``state.diarization_enabled`` before invoking this; the two gates are
    evaluated independently so that flipping the user preference cannot
    accidentally load a backend that isn't installed.
    """
    backend = os.environ.get(DIARIZER_ENV_VAR, "").strip().lower()
    if backend == "fake":
        return FakeDiarizer()
    if backend == "fluidaudio":
        from app.diarizer_fluidaudio import FluidAudioDiarizer
        return FluidAudioDiarizer()
    if backend:
        logger.warning(
            "Unknown diarizer backend %r in %s; running without diarization.",
            backend,
            DIARIZER_ENV_VAR,
        )
        return None

    # No explicit selection — auto-detect by checking for the Swift binary.
    # This is what ships to users: they enable diarization_enabled, and the
    # FluidAudio backend gets picked up if setup.command built its binary.
    from app.diarizer_fluidaudio import is_available as fluidaudio_available
    if fluidaudio_available():
        from app.diarizer_fluidaudio import FluidAudioDiarizer
        return FluidAudioDiarizer()
    return None
