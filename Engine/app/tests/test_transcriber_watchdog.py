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


class TestCleanupParakeet:
    @pytest.fixture(autouse=True)
    def _isolate_globals(self, monkeypatch):
        """Save + restore the module-level model/stream so tests don't bleed."""
        before_model = transcriber._parakeet_model
        before_stream = transcriber._parakeet_stream
        yield
        transcriber._parakeet_model = before_model
        transcriber._parakeet_stream = before_stream

    def test_idempotent_when_nothing_loaded(self, monkeypatch):
        transcriber._parakeet_model = None
        transcriber._parakeet_stream = None

        # Patch mlx.core.metal.clear_cache so a successful call would be
        # observable — but the function should short-circuit before calling it.
        called = []
        import sys
        fake_mx = types.ModuleType("mlx.core")
        fake_mx.metal = types.SimpleNamespace(
            clear_cache=lambda: called.append("clear"),
        )
        monkeypatch.setitem(sys.modules, "mlx.core", fake_mx)

        transcriber.cleanup_parakeet()  # Should be a no-op.
        assert called == [], "cleanup must short-circuit when nothing is loaded"

    def test_releases_globals_and_clears_cache(self, monkeypatch):
        transcriber._parakeet_model = "model-sentinel"
        transcriber._parakeet_stream = "stream-sentinel"

        called = []
        import sys
        fake_mx = types.ModuleType("mlx.core")
        fake_mx.metal = types.SimpleNamespace(
            clear_cache=lambda: called.append("clear"),
        )
        monkeypatch.setitem(sys.modules, "mlx.core", fake_mx)

        transcriber.cleanup_parakeet()

        assert transcriber._parakeet_model is None
        assert transcriber._parakeet_stream is None
        assert called == ["clear"]

    def test_clear_cache_failure_is_swallowed(self, monkeypatch):
        """Quit must always finish — a clear_cache exception can't propagate."""
        transcriber._parakeet_model = "x"
        transcriber._parakeet_stream = "y"

        def _boom():
            raise RuntimeError("metal driver unhappy")

        import sys
        fake_mx = types.ModuleType("mlx.core")
        fake_mx.metal = types.SimpleNamespace(clear_cache=_boom)
        monkeypatch.setitem(sys.modules, "mlx.core", fake_mx)

        # Should NOT raise. Globals still get nulled before the exception.
        transcriber.cleanup_parakeet()
        assert transcriber._parakeet_model is None
        assert transcriber._parakeet_stream is None
