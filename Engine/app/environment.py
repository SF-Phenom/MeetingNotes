"""environment.py — single source of truth for paths, binary locations, model expectations.

Originally these constants were spread across state.py, menubar.py, and a
few others. Centralizing them here makes the install layout a one-file
change later (e.g. when sharing with coworkers and the install path
might be ``/Applications/MeetingNotes.app/Contents/Resources`` instead of
``~/MeetingNotes``).

Bootstrap caveat: ``menubar.py`` and ``call_detector_worker.py`` each
duplicate the ``HOME_DIR`` derivation in their first few lines because
they need it BEFORE they can ``from app import ...`` (it's how they
extend ``sys.path``). That's bootstrap code, not a leak — the rest of
the app reads from here.
"""
from __future__ import annotations

import os


# --- Layout ------------------------------------------------------------------

HOME_DIR = os.environ.get(
    "MEETINGNOTES_HOME",
    os.path.expanduser("~/MeetingNotes"),
)

ENGINE_DIR = os.path.join(HOME_DIR, "Engine")
BIN_DIR = os.path.join(ENGINE_DIR, ".bin")
QUEUE_DIR = os.path.join(ENGINE_DIR, "recordings", "queue")
ACTIVE_DIR = os.path.join(ENGINE_DIR, "recordings", "active")
DONE_DIR = os.path.join(ENGINE_DIR, "recordings", "done")
TRANSCRIPTS_DIR = os.path.join(HOME_DIR, "transcripts")
SETTINGS_DIR = os.path.join(HOME_DIR, "Settings")
CONTEXT_PATH = os.path.join(SETTINGS_DIR, "context.md")

# Backwards-compat alias used by older code paths and tests. The app
# itself prefers HOME_DIR — BASE_DIR mirrors it for the few external
# call-sites we don't want to chase right now.
BASE_DIR = HOME_DIR


# --- Binary locations --------------------------------------------------------

# Native Swift recorder. Without this, recording is impossible — the
# precondition check below treats it as load-bearing.
CAPTURE_AUDIO_BIN = os.path.join(BIN_DIR, "capture-audio")

# Apple Speech transcriber bundle (built from Engine/SpeechTranscribe).
# Optional — only required when the user picks "apple_speech" as their
# transcription engine.
SPEECH_TRANSCRIBE_APP = os.path.join(BIN_DIR, "SpeechTranscribe.app")
SPEECH_TRANSCRIBE_BIN = os.path.join(
    SPEECH_TRANSCRIBE_APP, "Contents", "MacOS", "speech-transcribe",
)


# --- Model expectations ------------------------------------------------------

# Pulled by parakeet-mlx on first transcribe. Centralized here so a
# coworker building from a frozen model snapshot can override one place.
PARAKEET_MODEL_ID = "mlx-community/parakeet-tdt-0.6b-v3"

# Environment variable Claude reads for its API key. Surfaced as a
# constant so tests and setup scripts agree on the name.
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"


# --- Setup preconditions -----------------------------------------------------

def check_setup() -> list[str]:
    """Return a list of human-readable problems that would break the app.

    Empty list means setup is complete enough to run. The menubar surfaces
    the first item as a single "⚠ Setup incomplete — …" entry instead of
    the user discovering the problem mid-meeting.

    Only checks load-bearing components: the capture-audio binary (no
    recording without it) and the Engine directory itself. Optional
    pieces (Apple Speech bundle, Anthropic key, Ollama) are deliberately
    excluded — they have their own UI affordances and don't block the
    base "open the app, record, get a transcript" flow.
    """
    problems: list[str] = []

    if not os.path.isdir(ENGINE_DIR):
        problems.append(
            "Engine directory missing at {} — re-run setup.command.".format(
                ENGINE_DIR,
            )
        )
        # If the engine dir is gone, no point checking its contents.
        return problems

    if not (os.path.isfile(CAPTURE_AUDIO_BIN) and os.access(CAPTURE_AUDIO_BIN, os.X_OK)):
        problems.append(
            "capture-audio binary missing or not executable at {} — "
            "build Engine/CaptureAudio (see SETUP.md §10).".format(
                CAPTURE_AUDIO_BIN,
            )
        )

    return problems
