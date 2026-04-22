"""Tests for app.zoom_observer — subprocess lifecycle, flag resolution,
path derivation, and orphan recovery.

End-to-end tests that hit the real Swift binary (and therefore the AX
APIs) are out of scope — those need Zoom running and Accessibility
granted. Instead, we stub ZOOM_OBSERVER_BIN with short shell scripts
that mimic the behaviors we actually need to cover: permission probe
exit codes, SIGINT-responsive long-running subprocess, and the canned
JSONL writer used for pipeline fixtures.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time

import pytest

from app import zoom_observer as zo
from app.zoom_observer import (
    ACCESSIBILITY_PERMISSION_DENIED,
    AX_PARTICIPANTS_ENABLED_ENV_VAR,
    _sidecar_path_for,
    ax_participants_enabled,
    check_accessibility_permission,
    is_available,
    start_observer,
    stop_observer,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestSidecarPathFor:
    def test_strips_wav_and_appends_sidecar(self):
        assert _sidecar_path_for("/tmp/foo.wav") == "/tmp/foo.participants.jsonl"

    def test_path_without_wav_still_works(self):
        # Edge case — caller fed something that isn't a .wav. We shouldn't
        # mangle it into "/tmp/foo.bar.participants.jsonl"; splitext strips
        # just the trailing extension.
        assert _sidecar_path_for("/tmp/foo.bar") == "/tmp/foo.participants.jsonl"


class TestIsAvailable:
    def test_missing_binary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(zo, "ZOOM_OBSERVER_BIN", str(tmp_path / "nope"))
        assert is_available() is False

    def test_non_executable(self, monkeypatch, tmp_path):
        p = tmp_path / "observer"
        p.write_text("#!/bin/sh\nexit 0\n")
        # No chmod +x
        monkeypatch.setattr(zo, "ZOOM_OBSERVER_BIN", str(p))
        assert is_available() is False

    def test_executable(self, monkeypatch, tmp_path):
        p = tmp_path / "observer"
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
        monkeypatch.setattr(zo, "ZOOM_OBSERVER_BIN", str(p))
        assert is_available() is True


# ---------------------------------------------------------------------------
# ax_participants_enabled() — env var + state precedence
# ---------------------------------------------------------------------------


class TestAxParticipantsEnabled:
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch):
        monkeypatch.delenv(AX_PARTICIPANTS_ENABLED_ENV_VAR, raising=False)

    def test_env_on_wins_over_state_off(self, monkeypatch):
        # Simulate state.load returning ax_participants_enabled=False.
        class Stub:
            ax_participants_enabled = False
        monkeypatch.setattr("app.state.State.load", classmethod(lambda cls: Stub()))
        monkeypatch.setenv(AX_PARTICIPANTS_ENABLED_ENV_VAR, "1")
        assert ax_participants_enabled() is True

    def test_env_off_wins_over_state_on(self, monkeypatch):
        class Stub:
            ax_participants_enabled = True
        monkeypatch.setattr("app.state.State.load", classmethod(lambda cls: Stub()))
        monkeypatch.setenv(AX_PARTICIPANTS_ENABLED_ENV_VAR, "0")
        assert ax_participants_enabled() is False

    def test_state_on_no_env(self, monkeypatch):
        class Stub:
            ax_participants_enabled = True
        monkeypatch.setattr("app.state.State.load", classmethod(lambda cls: Stub()))
        assert ax_participants_enabled() is True

    def test_default_off_on_state_read_failure(self, monkeypatch, caplog):
        def boom(cls):
            raise RuntimeError("state.json corrupt")
        monkeypatch.setattr("app.state.State.load", classmethod(boom))
        with caplog.at_level("WARNING"):
            assert ax_participants_enabled() is False
        assert any("state.json corrupt" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# check_accessibility_permission() — subprocess exit-code branching
# ---------------------------------------------------------------------------


def _write_stub_binary(
    tmp_path,
    *,
    exit_code: int = 0,
    stderr: str = "",
    sleep_forever: bool = False,
) -> str:
    """Write a minimal Python stand-in for zoom-observer.

    When ``sleep_forever`` is True, the script installs SIGINT/SIGTERM
    handlers that call sys.exit(0), mimicking the real Swift binary's
    DispatchSource trap → exit(0) shutdown path. A /bin/sh stub would
    be shorter, but POSIX shells defer signal handlers until the
    foreground builtin returns, which makes the observed exit code
    race-prone against the test's wait(timeout=5).
    """
    p = tmp_path / "zoom-observer-stub"
    ready_file = tmp_path / "stub-ready"
    if sleep_forever:
        # Readiness sentinel pattern: the stub touches a file AFTER its
        # signal handlers are installed. The test polls for that file
        # before signalling, so SIGINT never races Python interpreter
        # startup (which can take >100ms under Gatekeeper on first run).
        script = (
            "#!/usr/bin/env python3\n"
            "import signal, sys, time\n"
            + (f"sys.stderr.write({stderr!r} + '\\n')\n" if stderr else "")
            + "def _bye(*_): sys.exit(0)\n"
            "signal.signal(signal.SIGINT, _bye)\n"
            "signal.signal(signal.SIGTERM, _bye)\n"
            f"open({str(ready_file)!r}, 'w').close()\n"
            "while True: time.sleep(0.05)\n"
        )
    else:
        script = (
            "#!/usr/bin/env python3\n"
            "import sys\n"
            + (f"sys.stderr.write({stderr!r} + '\\n')\n" if stderr else "")
            + f"sys.exit({exit_code})\n"
        )
    p.write_text(script)
    p.chmod(0o755)
    return str(p)


def _wait_for_stub_ready(tmp_path, timeout: float = 3.0) -> None:
    """Block until the stub binary has installed its signal handlers."""
    deadline = time.monotonic() + timeout
    ready_file = tmp_path / "stub-ready"
    while time.monotonic() < deadline:
        if ready_file.exists():
            return
        time.sleep(0.02)
    raise AssertionError(
        f"Stub did not signal readiness within {timeout}s (Python startup too slow?)"
    )


class TestCheckAccessibilityPermission:
    def test_granted_returns_true(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            zo, "ZOOM_OBSERVER_BIN", _write_stub_binary(tmp_path, exit_code=0),
        )
        assert check_accessibility_permission(timeout_secs=2.0) is True

    def test_denied_returns_false(self, monkeypatch, tmp_path, caplog):
        monkeypatch.setattr(
            zo, "ZOOM_OBSERVER_BIN",
            _write_stub_binary(tmp_path, exit_code=ACCESSIBILITY_PERMISSION_DENIED),
        )
        with caplog.at_level("WARNING"):
            assert check_accessibility_permission(timeout_secs=2.0) is False
        assert any(
            "Accessibility permission denied" in rec.message for rec in caplog.records
        )

    def test_missing_binary_returns_true(self, monkeypatch, tmp_path):
        # Caller should not be blocked by a probe that can't run; the
        # actual start path handles the no-binary case separately.
        monkeypatch.setattr(
            zo, "ZOOM_OBSERVER_BIN", str(tmp_path / "nonexistent"),
        )
        assert check_accessibility_permission(timeout_secs=1.0) is True

    def test_unknown_exit_code_returns_true(self, monkeypatch, tmp_path, caplog):
        # Exit codes other than 0 / 10 are "weird but don't block". Log
        # a warning so we notice; don't treat as denial.
        monkeypatch.setattr(
            zo, "ZOOM_OBSERVER_BIN",
            _write_stub_binary(tmp_path, exit_code=42, stderr="weird"),
        )
        with caplog.at_level("WARNING"):
            assert check_accessibility_permission(timeout_secs=2.0) is True


# ---------------------------------------------------------------------------
# start_observer / stop_observer — subprocess lifecycle via stub binary
# ---------------------------------------------------------------------------


class TestStartObserverGates:
    """Gating conditions that make start_observer return None."""

    def test_flag_off_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv(AX_PARTICIPANTS_ENABLED_ENV_VAR, "0")
        monkeypatch.setattr(
            zo, "ZOOM_OBSERVER_BIN",
            _write_stub_binary(tmp_path, sleep_forever=True),
        )
        assert start_observer(str(tmp_path / "rec.wav")) is None

    def test_binary_missing_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv(AX_PARTICIPANTS_ENABLED_ENV_VAR, "1")
        monkeypatch.setattr(zo, "ZOOM_OBSERVER_BIN", str(tmp_path / "nope"))
        assert start_observer(str(tmp_path / "rec.wav")) is None


class TestStartStopObserverWithStub:
    """Full lifecycle exercised with a shell-script stand-in."""

    @pytest.fixture
    def active_dir(self, tmp_path, monkeypatch):
        """Redirect ACTIVE_DIR (and the derived LOCK_FILE) at a tmp path."""
        d = tmp_path / "active"
        d.mkdir()
        monkeypatch.setattr(zo, "ACTIVE_DIR", str(d))
        monkeypatch.setattr(zo, "LOCK_FILE", str(d / ".zoom-observer.lock"))
        return d

    def test_start_writes_lockfile_and_stop_cleans_up(
        self, monkeypatch, tmp_path, active_dir,
    ):
        monkeypatch.setenv(AX_PARTICIPANTS_ENABLED_ENV_VAR, "1")
        monkeypatch.setattr(
            zo, "ZOOM_OBSERVER_BIN",
            _write_stub_binary(tmp_path, sleep_forever=True),
        )

        wav = tmp_path / "zoom_2026-04-22_09-30.wav"
        proc = start_observer(str(wav))
        assert proc is not None
        # Lockfile written with the child PID.
        lock = active_dir / ".zoom-observer.lock"
        assert lock.exists()
        assert int(lock.read_text()) == proc.pid

        # Block until the stub has installed its SIGINT handler — timing
        # alone is unreliable under macOS Gatekeeper's first-run latency.
        _wait_for_stub_ready(tmp_path)
        stop_observer(proc)
        # Stub exits on SIGINT via its handler → returncode 0.
        assert proc.returncode == 0
        # Lockfile removed.
        assert not lock.exists()

    def test_stop_is_noop_on_none(self, active_dir):
        # Observer never launched (e.g. non-Zoom source) — stop_observer
        # must tolerate None without raising.
        stop_observer(None)  # should not raise

    def test_stop_on_already_exited_proc_is_noop(
        self, monkeypatch, tmp_path, active_dir,
    ):
        monkeypatch.setenv(AX_PARTICIPANTS_ENABLED_ENV_VAR, "1")
        # Stub exits immediately so the Popen has already terminated
        # by the time we call stop_observer.
        monkeypatch.setattr(
            zo, "ZOOM_OBSERVER_BIN", _write_stub_binary(tmp_path, exit_code=0),
        )
        proc = start_observer(str(tmp_path / "rec.wav"))
        assert proc is not None
        proc.wait(timeout=2)
        # stop_observer on an already-exited process should not raise
        # and should clean up the lockfile.
        stop_observer(proc)
        assert not (active_dir / ".zoom-observer.lock").exists()


# ---------------------------------------------------------------------------
# recover_orphan() — dead-pid lockfile cleanup
# ---------------------------------------------------------------------------


class TestRecoverOrphan:
    @pytest.fixture
    def active_dir(self, tmp_path, monkeypatch):
        d = tmp_path / "active"
        d.mkdir()
        monkeypatch.setattr(zo, "ACTIVE_DIR", str(d))
        monkeypatch.setattr(zo, "LOCK_FILE", str(d / ".zoom-observer.lock"))
        return d

    def test_no_lockfile_is_noop(self, active_dir):
        zo.recover_orphan()  # should not raise

    def test_dead_pid_clears_lock(self, active_dir, caplog):
        # Spawn a child, let it exit, reuse its now-dead pid. Reliable
        # across platforms where "an obviously impossible pid" is harder
        # to define (PID 0 has POSIX special meaning — signals sent to 0
        # go to the whole process group and don't raise ProcessLookupError).
        sub = subprocess.Popen(
            ["/usr/bin/env", "python3", "-c", "pass"],
        )
        sub.wait()
        lock = active_dir / ".zoom-observer.lock"
        lock.write_text(str(sub.pid))
        with caplog.at_level("WARNING"):
            zo.recover_orphan()
        assert not lock.exists()
        assert any("Orphaned zoom-observer" in rec.message for rec in caplog.records)

    def test_garbage_lockfile_is_cleared(self, active_dir, caplog):
        # Corrupt lockfile (non-integer) → we log and treat as orphan.
        lock = active_dir / ".zoom-observer.lock"
        lock.write_text("not-a-pid")
        with caplog.at_level("WARNING"):
            zo.recover_orphan()
        assert not lock.exists()

    def test_live_pid_preserved(self, active_dir):
        # Our own pid is definitely alive — recover_orphan must leave
        # the lockfile intact in that case.
        lock = active_dir / ".zoom-observer.lock"
        lock.write_text(str(os.getpid()))
        zo.recover_orphan()
        assert lock.exists()
