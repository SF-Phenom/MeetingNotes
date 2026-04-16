"""Tests for the call-detector proxy + worker.

The worker's ``_handle`` is tested directly (pure dispatch). The proxy is
tested end-to-end against a fake worker script that scripts its responses —
this verifies the protocol, timeout handling, and restart-on-crash without
dragging osascript/psutil into the test path.
"""
from __future__ import annotations

import json
import sys
import textwrap

import pytest

from app import call_detector_worker


# ---------------------------------------------------------------------------
# Worker handler — pure function, no subprocess involved.
# ---------------------------------------------------------------------------


class TestWorkerHandler:
    def test_ping(self):
        assert call_detector_worker._handle({"cmd": "ping"}) == {"pong": True}

    def test_detect_delegates(self, monkeypatch):
        monkeypatch.setattr(
            call_detector_worker.call_detector,
            "detect_active_call",
            lambda: {"source": "zoom", "url": None},
        )
        assert call_detector_worker._handle({"cmd": "detect"}) == {
            "result": {"source": "zoom", "url": None},
        }

    def test_alive_delegates(self, monkeypatch):
        captured = []

        def fake(source):
            captured.append(source)
            return True

        monkeypatch.setattr(
            call_detector_worker.call_detector, "is_call_still_active", fake,
        )
        assert call_detector_worker._handle({"cmd": "alive", "source": "zoom"}) == {
            "alive": True,
        }
        assert captured == ["zoom"]

    def test_unknown_command(self):
        result = call_detector_worker._handle({"cmd": "fly"})
        assert "error" in result
        assert "fly" in result["error"]


# ---------------------------------------------------------------------------
# Proxy end-to-end against a fake worker.
# ---------------------------------------------------------------------------


FAKE_WORKER_ECHO = textwrap.dedent('''\
    import json
    import sys
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        cmd = json.loads(line)
        if cmd.get("cmd") == "ping":
            sys.stdout.write(json.dumps({"pong": True}) + "\\n")
        elif cmd.get("cmd") == "detect":
            sys.stdout.write(json.dumps({"result": {"source": "test", "url": None}}) + "\\n")
        elif cmd.get("cmd") == "alive":
            sys.stdout.write(json.dumps({"alive": True}) + "\\n")
        else:
            sys.stdout.write(json.dumps({"error": "?"}) + "\\n")
        sys.stdout.flush()
''')


FAKE_WORKER_CRASH_IMMEDIATELY = textwrap.dedent('''\
    import sys
    sys.exit(1)
''')


FAKE_WORKER_HANG = textwrap.dedent('''\
    import sys, time
    # Read one line then sleep forever so the proxy times out.
    sys.stdin.readline()
    time.sleep(60)
''')


@pytest.fixture
def proxy_with_fake_worker(monkeypatch, tmp_path):
    """Factory: writes a worker script, points the proxy at it, returns the proxy."""
    from app import call_detector_proxy

    def _make(source: str, response_timeout: float = 5.0):
        script = tmp_path / "fake_worker.py"
        script.write_text(source, encoding="utf-8")

        monkeypatch.setattr(
            call_detector_proxy, "WORKER_SCRIPT", str(script), raising=True,
        )
        monkeypatch.setattr(
            call_detector_proxy,
            "WORKER_RESPONSE_TIMEOUT_SECS",
            response_timeout,
            raising=True,
        )
        proxy = call_detector_proxy.CallDetectorProxy()
        return proxy

    return _make


class TestCallDetectorProxy:
    def test_ping_roundtrip(self, proxy_with_fake_worker):
        proxy = proxy_with_fake_worker(FAKE_WORKER_ECHO)
        try:
            assert proxy.ping() is True
        finally:
            proxy.stop()

    def test_detect_returns_worker_result(self, proxy_with_fake_worker):
        proxy = proxy_with_fake_worker(FAKE_WORKER_ECHO)
        try:
            assert proxy.detect_active_call() == {"source": "test", "url": None}
        finally:
            proxy.stop()

    def test_alive_returns_worker_result(self, proxy_with_fake_worker):
        proxy = proxy_with_fake_worker(FAKE_WORKER_ECHO)
        try:
            assert proxy.is_call_still_active("zoom") is True
        finally:
            proxy.stop()

    def test_missing_script_degrades_gracefully(self, monkeypatch):
        """With the worker script missing, operations return safe defaults."""
        from app import call_detector_proxy

        monkeypatch.setattr(
            call_detector_proxy, "WORKER_SCRIPT", "/nonexistent/script.py",
            raising=True,
        )
        proxy = call_detector_proxy.CallDetectorProxy()
        try:
            assert proxy.detect_active_call() is None
            # is_call_still_active defaults to True on failure so recording
            # keeps going rather than stopping unexpectedly.
            assert proxy.is_call_still_active("zoom") is True
            assert proxy.ping() is False
        finally:
            proxy.stop()

    def test_worker_crash_handled(self, proxy_with_fake_worker):
        """If the worker crashes mid-session, operations return safe defaults
        (and the next call will respawn)."""
        proxy = proxy_with_fake_worker(FAKE_WORKER_CRASH_IMMEDIATELY)
        try:
            # Worker exits before responding — should degrade, not raise.
            assert proxy.detect_active_call() is None
            assert proxy.is_call_still_active("zoom") is True
        finally:
            proxy.stop()

    def test_hang_respects_timeout(self, proxy_with_fake_worker):
        """A hung worker must not block the proxy indefinitely."""
        proxy = proxy_with_fake_worker(FAKE_WORKER_HANG, response_timeout=0.5)
        try:
            # Should time out and return None within ~0.5s, not hang forever.
            import time
            t0 = time.time()
            result = proxy.detect_active_call()
            elapsed = time.time() - t0
            assert result is None
            assert elapsed < 3.0, f"proxy did not time out quickly enough: {elapsed}s"
        finally:
            proxy.stop()

    def test_restart_after_crash(self, proxy_with_fake_worker, tmp_path, monkeypatch):
        """After a worker crash, the next call should respawn and succeed."""
        from app import call_detector_proxy

        # Write two versions of the worker and rewrite the script path between
        # calls to simulate a crash followed by a recovery.
        script = tmp_path / "worker.py"
        script.write_text(FAKE_WORKER_CRASH_IMMEDIATELY, encoding="utf-8")
        monkeypatch.setattr(
            call_detector_proxy, "WORKER_SCRIPT", str(script), raising=True,
        )
        monkeypatch.setattr(
            call_detector_proxy, "WORKER_RESPONSE_TIMEOUT_SECS", 2.0, raising=True,
        )
        proxy = call_detector_proxy.CallDetectorProxy()
        try:
            # First call: worker dies immediately.
            assert proxy.ping() is False
            # Replace the script with a working echo and try again.
            script.write_text(FAKE_WORKER_ECHO, encoding="utf-8")
            assert proxy.ping() is True
        finally:
            proxy.stop()
