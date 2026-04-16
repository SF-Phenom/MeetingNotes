"""RecordingFile — a .wav and its sidecar family as a single unit.

A meeting recording is more than the mic .wav. Depending on context there
may also be:

  - ``<base>.sys.wav`` — system-audio track from ScreenCaptureKit
  - ``<base>.meta.json`` — structured metadata (calendar enrichment, source)
  - ``<base>.srt`` — legacy captions file (whisper-era; Parakeet doesn't
    produce these but they may linger on disk)

Before this class, three modules (recorder, pipeline, cleanup) each
hand-rolled the glob / move / delete logic with subtle bugs:
cleanup.py didn't touch .sys.wav, and recorder's orphan path dropped
.meta.json. Funnel them all through one aggregate so a fix made here lands
everywhere.
"""
from __future__ import annotations

import logging
import os
import shutil

logger = logging.getLogger(__name__)


# Extensions that sit alongside the main .wav. Order matters only for logging.
SIDECAR_EXTENSIONS: tuple[str, ...] = (".sys.wav", ".meta.json", ".srt")


class RecordingFile:
    """The .wav + its sidecars, treated as one thing."""

    def __init__(self, wav_path: str) -> None:
        self._wav_path = os.path.expanduser(wav_path)
        # Strip only the trailing ``.wav`` — without that, a ``foo.sys.wav``
        # accidentally passed in would get ``foo.sys`` as the base and miss
        # its own siblings. Callers should pass the mic .wav, not the .sys.wav.
        if self._wav_path.endswith(".wav"):
            self._base = self._wav_path[:-4]
        else:
            self._base = os.path.splitext(self._wav_path)[0]

    # -- Paths ----------------------------------------------------------------

    @property
    def wav_path(self) -> str:
        return self._wav_path

    @property
    def system_audio_path(self) -> str:
        return self._base + ".sys.wav"

    @property
    def metadata_path(self) -> str:
        return self._base + ".meta.json"

    @property
    def srt_path(self) -> str:
        return self._base + ".srt"

    @property
    def basename(self) -> str:
        return os.path.basename(self._wav_path)

    # -- Existence ------------------------------------------------------------

    def existing_files(self) -> list[str]:
        """Every related path that actually exists on disk.

        Always includes the main .wav first if present, followed by any
        sidecars in SIDECAR_EXTENSIONS order. Non-existent paths are
        omitted.
        """
        candidates = [self._wav_path] + [
            self._base + ext for ext in SIDECAR_EXTENSIONS
        ]
        return [p for p in candidates if os.path.exists(p)]

    # -- Mutations ------------------------------------------------------------

    def delete(self) -> int:
        """Delete .wav and every existing sidecar. Returns count removed."""
        count = 0
        for path in self.existing_files():
            try:
                os.remove(path)
                logger.info("Deleted: %s", path)
                count += 1
            except OSError as e:
                logger.warning("Could not delete %s: %s", path, e)
        return count

    def move_to(self, dest_dir: str) -> "RecordingFile":
        """Move .wav and every existing sidecar into ``dest_dir``.

        Creates ``dest_dir`` if needed. Returns a new RecordingFile pointing
        at the new location. Raises OSError if the main .wav cannot be moved
        (sidecar failures are logged but don't abort the operation — they'd
        leave the user with a correctly-moved .wav and orphaned sidecars,
        which is strictly better than rolling everything back).
        """
        os.makedirs(dest_dir, exist_ok=True)
        new_wav = os.path.join(dest_dir, os.path.basename(self._wav_path))

        existing = self.existing_files()
        # Move the main .wav first — if it fails, surface the error before
        # we move any sidecars.
        if self._wav_path in existing:
            shutil.move(self._wav_path, new_wav)
            existing.remove(self._wav_path)

        for src in existing:
            dst = os.path.join(dest_dir, os.path.basename(src))
            try:
                shutil.move(src, dst)
            except OSError as e:
                logger.error(
                    "Could not move sidecar %s -> %s: %s (main .wav moved OK)",
                    src, dst, e,
                )
        return RecordingFile(new_wav)
