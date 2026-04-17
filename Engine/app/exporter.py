"""Action-item exporter — fan out summary action items to a task system.

Mirrors transcription_engine's shape: a thin dispatch module with
backend-specific implementations in ``app.exporters.*``. The pipeline
calls ``export_action_items`` after the transcript has been written;
backends are responsible for de-duplication if they care (Apple Reminders
deliberately doesn't — duplicate items in a list are easy to spot and
merge later, easier than reasoning about cross-meeting identity here).

Backends supported today:
  - ``disabled`` — default; no-op.
  - ``apple_reminders`` — AppleScript bridge to Reminders.app.

Order matches Phase 5 of the refactor plan; Things 3, Google Tasks, and
Notion are queued behind this one and slot in by adding a branch to
``_dispatch`` plus a new module under ``app.exporters``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app import state as state_mod

logger = logging.getLogger(__name__)


BACKEND_DISABLED = "disabled"
BACKEND_APPLE_REMINDERS = "apple_reminders"
SUPPORTED_BACKENDS: tuple[str, ...] = (BACKEND_DISABLED, BACKEND_APPLE_REMINDERS)


@dataclass
class ExportResult:
    """Outcome of one exporter run, surfaced to the UI."""

    backend: str
    exported_count: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def attempted(self) -> bool:
        """True if a backend actually ran (i.e. not disabled and given items)."""
        return self.backend != BACKEND_DISABLED


# ---------------------------------------------------------------------------
# Preference helpers (state-backed)
# ---------------------------------------------------------------------------

def get_backend_preference() -> str:
    """Read the persisted backend choice. Falls back to ``disabled``."""
    return str(
        state_mod.load().get("exporter_backend", BACKEND_DISABLED)
    ).strip().lower()


def set_backend_preference(name: str) -> None:
    """Persist the user's backend choice. Validates against SUPPORTED_BACKENDS."""
    name = name.strip().lower()
    if name not in SUPPORTED_BACKENDS:
        raise ValueError(
            "Unknown exporter backend: {!r}. Supported: {}.".format(
                name, ", ".join(SUPPORTED_BACKENDS),
            )
        )
    state_mod.update(exporter_backend=name)
    logger.info("Exporter backend preference set to %s", name)


def is_backend_available(name: str) -> bool:
    """True if the backend can actually run on this machine.

    Apple Reminders is always available on macOS — we don't pre-flight the
    Reminders app here because doing so is itself an AppleScript call.
    Failures surface in ExportResult.errors at export time, which is more
    actionable than a startup crash.
    """
    name = name.strip().lower()
    return name in SUPPORTED_BACKENDS


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def export_action_items(
    action_items: list[dict],
    metadata: dict | None = None,
) -> ExportResult:
    """Send action items to the configured backend.

    Args:
        action_items: List of ``{"item": str, "owner": str|None, "due": str|None}``
            dicts as produced by the summarizer.
        metadata: Pipeline metadata (title, source, date) used to label the
            origin of each exported item. May be None.

    Returns:
        ExportResult — non-fatal: callers should log errors but not fail
        the pipeline. The transcript is already on disk by the time this
        is called.
    """
    backend = get_backend_preference()
    if backend == BACKEND_DISABLED:
        return ExportResult(backend=BACKEND_DISABLED)

    if not action_items:
        # Distinct from "disabled" so the UI can stay quiet — nothing to do.
        return ExportResult(backend=backend, exported_count=0)

    return _dispatch(backend, action_items, metadata or {})


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _dispatch(backend: str, action_items: list[dict], metadata: dict) -> ExportResult:
    if backend == BACKEND_APPLE_REMINDERS:
        # Late import — keeps the module importable even when subprocess
        # / osascript aren't relevant (e.g. in unit tests for unrelated
        # backends).
        from app.exporters import apple_reminders
        list_name = str(
            state_mod.load().get("apple_reminders_list", "MeetingNotes")
        ).strip() or "MeetingNotes"
        source_label = _label_for(metadata)
        count, errors = apple_reminders.add_items(
            action_items, list_name=list_name, source_label=source_label,
        )
        return ExportResult(
            backend=backend, exported_count=count, errors=list(errors),
        )

    return ExportResult(
        backend=backend,
        errors=["Unknown exporter backend: {!r}".format(backend)],
    )


def _label_for(metadata: dict) -> str:
    """Build a one-line origin label for exported items.

    Goes into each item's note/body so you can find the source meeting
    later. Falls back to source+date when no title is available.
    """
    title = metadata.get("title")
    source = metadata.get("source", "meeting")
    date = metadata.get("date") or metadata.get("date_str")
    if title:
        return str(title)
    if date:
        return "{} ({})".format(source, date)
    return str(source)
