"""Tests for the GPU-synchronize watchdog helper in transcriber.py.

These patch out mlx.core so they run without the real MLX/Metal stack —
we're testing the Timer plumbing, not GPU behavior.
"""
from __future__ import annotations

import threading
import time
import types

import pytest

from app import transcriber


class _FakeMLX:
    """Minimal stand-in for mlx.core used inside synchronize_with_watchdog."""

    def __init__(self, sleep_secs: float):
        self._sleep_secs = sleep_secs
        self.sync_called_with = None

    def synchronize(self, stream):
        self.sync_called_with = stream
        time.sleep(self._sleep_secs)


@pytest.fixture
def patch_mlx(monkeypatch):
    """Install a fake `mlx.core` module so the watchdog can be exercised."""
    def _install(sleep_secs: float) -> _FakeMLX:
        fake = _FakeMLX(sleep_secs=sleep_secs)
        fake_module = types.ModuleType("mlx.core")
        fake_module.synchronize = fake.synchronize
        import sys
        # Put the fake in place of mlx.core for the duration of the test.
        monkeypatch.setitem(sys.modules, "mlx.core", fake_module)
        # Also cover `mlx` in case something does `import mlx`
        parent = sys.modules.get("mlx")
        if parent is None:
            parent = types.ModuleType("mlx")
            monkeypatch.setitem(sys.modules, "mlx", parent)
        parent.core = fake_module
        return fake

    return _install


class TestSynchronizeWithWatchdog:
    def test_completes_without_firing(self, patch_mlx):
        fake = patch_mlx(sleep_secs=0.0)
        fired = threading.Event()
        transcriber.synchronize_with_watchdog(
            "stream-sentinel",
            name="test",
            on_timeout=fired.set,
            timeout_secs=1.0,
        )
        assert fake.sync_called_with == "stream-sentinel"
        assert not fired.is_set(), "watchdog should not fire on a fast sync"

    def test_fires_on_slow_sync_and_invokes_callback(self, patch_mlx):
        # Sync sleeps 0.3s; watchdog fires at 0.1s — callback must run.
        patch_mlx(sleep_secs=0.3)
        fired = threading.Event()
        transcriber.synchronize_with_watchdog(
            "stream",
            name="test",
            on_timeout=fired.set,
            timeout_secs=0.1,
        )
        assert fired.is_set(), "watchdog should have fired and set the event"

    def test_timer_cancelled_after_completion(self, patch_mlx):
        """A slow-enough sync that completes before a long timeout should not
        leave a timer running — verify by calling twice in quick succession."""
        patch_mlx(sleep_secs=0.0)
        fired = threading.Event()
        for _ in range(3):
            transcriber.synchronize_with_watchdog(
                "stream",
                name="test",
                on_timeout=fired.set,
                timeout_secs=5.0,
            )
        assert not fired.is_set()

    def test_callback_exception_is_logged_not_raised(self, patch_mlx):
        """A misbehaving on_timeout callback must not crash the caller."""
        patch_mlx(sleep_secs=0.2)

        def _boom():
            raise RuntimeError("user callback bug")

        # Should not raise.
        transcriber.synchronize_with_watchdog(
            "stream",
            name="test",
            on_timeout=_boom,
            timeout_secs=0.05,
        )

    def test_no_callback_still_logs_and_returns(self, patch_mlx):
        patch_mlx(sleep_secs=0.2)
        # Should not raise even without on_timeout.
        transcriber.synchronize_with_watchdog(
            "stream",
            name="test",
            timeout_secs=0.05,
        )
