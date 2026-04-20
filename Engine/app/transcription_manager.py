"""TranscriptionManager — realtime + background-batch transcription lifecycle.

Owns the RealtimeTranscriber instance, the batch worker thread, and the
``is_transcribing`` flag (now behind a Lock). A hang or crash inside
pipeline.process_recording used to wedge that flag permanently — the
try/finally here guarantees it always clears.

Stays rumps-free by design. The menubar injects two callables:

  ``rebuild_menu()``      — recomputes and applies the menu state. Called
                            whenever is_transcribing flips so the "…"
                            spinner appears/disappears.
  ``notify(subtitle, msg)`` — shows a rumps notification. Called for
                            completion / failure / check-in-ready.

Both callbacks are invoked *through* a UIBridge so they land on the main
rumps thread regardless of which thread the transcription work itself is
running on.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Callable, NamedTuple

from app import pipeline
from app import checkin
from app.transcription_engine import RealtimeEngine, get_realtime_engine
from app.ui_bridge import UIBridge

logger = logging.getLogger(__name__)

RebuildMenuFn = Callable[[], None]
NotifyFn = Callable[[str, str], None]


class RealtimeResult(NamedTuple):
    """Outcome of stopping a realtime session.

    Carries both the accumulated text (used for the existing "too short"
    check and for the Apple Speech path, which has no per-sentence
    timings) and the list of :class:`~app.transcript_formatter.Sentence`
    records (used by pipeline-level diarization when diarization is
    enabled). Either field may be empty independently — a short recording
    yields "", []; Apple Speech yields "some text", [].
    """

    text: str | None
    sentences: list


class TranscriptionManager:
    """Owns realtime + batch transcription."""

    def __init__(
        self,
        ui_bridge: UIBridge,
        rebuild_menu: RebuildMenuFn,
        notify: NotifyFn,
    ) -> None:
        self._ui_bridge = ui_bridge
        self._rebuild_menu = rebuild_menu
        self._notify = notify

        self._realtime: RealtimeEngine | None = None
        self._lock = threading.Lock()
        self._transcribing = False

    # -- Realtime lifecycle ---------------------------------------------------

    def start_realtime(self, wav_path: str) -> bool:
        """Begin live transcription of ``wav_path`` as it grows on disk.

        Returns True on success, False if the realtime engine failed to
        start (batch transcription is still usable in that case).
        """
        try:
            rt = get_realtime_engine()
            rt.start(wav_path)
            self._realtime = rt
            logger.info("Realtime transcriber started")
            return True
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to start realtime transcriber: %s", e, exc_info=True)
            self._realtime = None
            return False

    def stop_realtime(self) -> RealtimeResult:
        """Stop live transcription and return both the accumulated text
        AND any per-sentence timings the engine produced.

        Always returns a :class:`RealtimeResult`; missing or erroring
        engines come back as ``RealtimeResult(None, [])``. Sentences are
        pulled off the engine *before* the reference is cleared so that
        callers (menubar → submit()) can pass them to the pipeline for
        diarization alignment.
        """
        if self._realtime is None:
            return RealtimeResult(text=None, sentences=[])
        try:
            text = self._realtime.stop()
            # accumulated_sentences is a RealtimeEngine-protocol member —
            # Parakeet returns the corrected list; Apple Speech returns [].
            sentences = list(self._realtime.accumulated_sentences)
            logger.info(
                "Realtime transcriber stopped, got %d chars, %d sentences",
                len(text) if text else 0,
                len(sentences),
            )
            return RealtimeResult(text=text, sentences=sentences)
        except Exception as e:  # noqa: BLE001
            logger.error("Error stopping realtime transcriber: %s", e)
            return RealtimeResult(text=None, sentences=[])
        finally:
            self._realtime = None

    @property
    def realtime_live_transcript_path(self) -> str | None:
        """Path to the live .txt file the realtime engine writes, or None."""
        return self._realtime.live_transcript_path if self._realtime else None

    # -- Batch lifecycle ------------------------------------------------------

    @property
    def is_transcribing(self) -> bool:
        with self._lock:
            return self._transcribing

    def submit(
        self,
        wav_paths: list[str],
        pre_transcribed_text: str | None = None,
        pre_transcribed_sentences: list | None = None,
    ) -> bool:
        """Queue a batch of recordings for background transcription.

        ``pre_transcribed_sentences`` (when non-empty) carries the realtime
        engine's accumulated Sentence list so the pipeline can run
        diarization on it without re-reading the WAV. Applies only to the
        first wav in ``wav_paths`` — subsequent files weren't seen by the
        realtime engine and will take the batch path if Parakeet is
        selected.

        Returns True if the job started, False if another batch is already
        running (caller can ignore or surface a message).
        """
        with self._lock:
            if self._transcribing:
                logger.warning("Transcription already in progress, skipping")
                return False
            self._transcribing = True

        # Immediate menu rebuild so the UI reflects the "Transcribing..." state.
        # Dispatched through the UI bridge so we're safe no matter which
        # thread called submit().
        self._ui_bridge.dispatch(self._rebuild_menu)

        thread = threading.Thread(
            target=self._run_batch,
            args=(wav_paths, pre_transcribed_text, pre_transcribed_sentences),
            daemon=True,
        )
        thread.start()
        return True

    def shutdown(self) -> None:
        """Called on app quit — stop realtime if running."""
        self.stop_realtime()

    # -- Internals ------------------------------------------------------------

    def _run_batch(
        self,
        wav_paths: list[str],
        pre_transcribed_text: str | None,
        pre_transcribed_sentences: list | None = None,
    ) -> None:
        succeeded = 0
        failed = 0
        too_short = 0
        fell_back = False
        last_path: str | None = None
        exported_total = 0
        export_errors: list[str] = []
        # Per-iteration flag: pipeline fires on_too_short before returning None,
        # so we distinguish "skipped because too short" from "failed for real"
        # without changing the pipeline's return type.
        saw_too_short = False

        def _mark_fallback() -> None:
            nonlocal fell_back
            fell_back = True

        def _record_export(result) -> None:
            nonlocal exported_total
            exported_total += result.exported_count
            export_errors.extend(result.errors)

        def _mark_too_short() -> None:
            nonlocal saw_too_short
            saw_too_short = True

        try:
            for i, wav_path in enumerate(wav_paths):
                saw_too_short = False
                try:
                    logger.info("Transcribing: %s", os.path.basename(wav_path))
                    # Only the first file gets pre-transcribed text + sentences
                    # from realtime; subsequent files weren't seen by realtime.
                    pre_text = pre_transcribed_text if i == 0 else None
                    pre_sents = pre_transcribed_sentences if i == 0 else None
                    result = pipeline.process_recording(
                        wav_path,
                        pre_transcribed_text=pre_text,
                        pre_transcribed_sentences=pre_sents,
                        on_summary_fallback=_mark_fallback,
                        on_export=_record_export,
                        on_too_short=_mark_too_short,
                    )
                    if result:
                        succeeded += 1
                        last_path = result
                    elif saw_too_short:
                        too_short += 1
                    else:
                        failed += 1
                except Exception as e:
                    logger.error(
                        "Pipeline error for %s: %s", wav_path, e, exc_info=True,
                    )
                    failed += 1
        finally:
            # Always clear the flag — a hang in pipeline.process_recording
            # used to wedge _transcribing=True permanently and block future
            # transcriptions until app restart.
            with self._lock:
                self._transcribing = False

        # UI updates back on the main thread.
        self._ui_bridge.dispatch(self._rebuild_menu)

        if succeeded > 0:
            subtitle = "{} transcript{} ready".format(
                succeeded, "s" if succeeded > 1 else "",
            )
            message = os.path.basename(last_path) if last_path else ""
            self._ui_bridge.dispatch(
                lambda sub=subtitle, msg=message: self._notify(sub, msg)
            )
        if fell_back:
            self._ui_bridge.dispatch(
                lambda: self._notify(
                    "Summarized with local model",
                    "Claude was unavailable; used Ollama. Quality may differ.",
                )
            )
        if exported_total > 0:
            self._ui_bridge.dispatch(
                lambda n=exported_total: self._notify(
                    "Action items exported",
                    "{} item{} sent to your task system.".format(
                        n, "s" if n != 1 else "",
                    ),
                )
            )
        if export_errors:
            first = export_errors[0]
            self._ui_bridge.dispatch(
                lambda msg=first: self._notify(
                    "Action item export failed", msg,
                )
            )
        if failed > 0:
            failed_msg = "{} recording{} failed — check logs".format(
                failed, "s" if failed > 1 else "",
            )
            self._ui_bridge.dispatch(
                lambda msg=failed_msg: self._notify("Transcription errors", msg)
            )
        if too_short > 0:
            too_short_msg = "{} recording{} under 16 sec; nothing to transcribe".format(
                too_short, "s" if too_short > 1 else "",
            )
            self._ui_bridge.dispatch(
                lambda msg=too_short_msg: self._notify("Recording too short", msg)
            )
        if succeeded > 0 and checkin.should_trigger_checkin():
            self._ui_bridge.dispatch(
                lambda: self._notify(
                    "Check-in ready",
                    "You have enough new transcripts for a project check-in.",
                )
            )
