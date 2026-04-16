"""Main-process proxy for the call-detector worker subprocess.

Sends JSON commands over stdin/stdout to a long-lived
``call_detector_worker.py`` child. The worker runs osascript/psutil calls so
the main menubar process never forks while Parakeet/MLX GPU threads are
active (which would corrupt malloc locks in the child).

Failure modes are tolerated quietly: if the worker crashes, respawn it on
the next call; if the respawn itself fails, detection degrades to "no call
detected" rather than crashing the menubar.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading

from .state import BASE_DIR

logger = logging.getLogger(__name__)

WORKER_SCRIPT = os.path.join(BASE_DIR, "Engine", "app", "call_detector_worker.py")

# Cap how long we'll wait for a worker response. osascript calls inside the
# worker already have their own timeouts (5s), so this is belt-and-suspenders
# against a wedged worker. Longer than the worker's own worst case.
WORKER_RESPONSE_TIMEOUT_SECS = 20.0


class CallDetectorProxy:
    """Spawns and talks to the detector worker. Thread-safe."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stderr_thread: threading.Thread | None = None

    # -- Lifecycle ------------------------------------------------------------

    def start(self) -> None:
        """Spawn the worker if it isn't already running."""
        with self._lock:
            self._ensure_running_locked()

    def stop(self) -> None:
        """Terminate the worker by closing its stdin and waiting briefly."""
        with self._lock:
            if self._proc is None:
                return
            try:
                if self._proc.stdin is not None:
                    self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=1)
                except (subprocess.TimeoutExpired, OSError):
                    try:
                        self._proc.kill()
                    except OSError:
                        pass
            self._proc = None

    # -- Public API -----------------------------------------------------------

    def detect_active_call(self) -> dict | None:
        """Run the full detection sweep in the worker. Returns None on any failure."""
        response = self._send({"cmd": "detect"})
        if response is None:
            return None
        return response.get("result")

    def is_call_still_active(self, source: str) -> bool:
        """Lightweight is-still-there check. Defaults to True if worker fails
        (the existing caller interprets True as 'don't stop recording')."""
        response = self._send({"cmd": "alive", "source": source})
        if response is None:
            return True
        return bool(response.get("alive", True))

    def ping(self) -> bool:
        """Sanity check — returns True if the worker responds with pong."""
        response = self._send({"cmd": "ping"})
        return bool(response and response.get("pong"))

    # -- Internals ------------------------------------------------------------

    def _ensure_running_locked(self) -> None:
        """Start or restart the worker. Caller must hold self._lock."""
        if self._proc is not None and self._proc.poll() is None:
            return

        if self._proc is not None:
            logger.warning(
                "call detector worker died (exit=%s); respawning",
                self._proc.returncode,
            )
            self._proc = None

        if not os.path.exists(WORKER_SCRIPT):
            logger.error("call detector worker script missing: %s", WORKER_SCRIPT)
            return

        env = dict(os.environ)
        env["PYTHONUNBUFFERED"] = "1"

        try:
            self._proc = subprocess.Popen(
                [sys.executable, WORKER_SCRIPT],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as e:
            logger.error("Failed to spawn call detector worker: %s", e)
            self._proc = None
            return

        # Drain stderr into our logger so worker crashes don't fill its pipe
        # (and hang it on write).
        def _log_stderr(stream, prefix: str) -> None:
            try:
                for line in stream:
                    line = line.rstrip()
                    if line:
                        logger.info("%s: %s", prefix, line)
            except Exception:
                pass

        self._stderr_thread = threading.Thread(
            target=_log_stderr,
            args=(self._proc.stderr, "call-detector-worker"),
            daemon=True,
        )
        self._stderr_thread.start()

        logger.info("call detector worker spawned (pid=%d)", self._proc.pid)

    def _send(self, cmd: dict) -> dict | None:
        """Send one command; return the decoded response, or None on failure."""
        with self._lock:
            self._ensure_running_locked()
            if self._proc is None or self._proc.stdin is None or self._proc.stdout is None:
                return None

            try:
                self._proc.stdin.write(json.dumps(cmd) + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                logger.warning("worker stdin write failed: %s", e)
                self._proc = None
                return None

            line = self._read_line_with_timeout(
                self._proc.stdout, WORKER_RESPONSE_TIMEOUT_SECS,
            )
            if line is None:
                # Timeout or pipe closed — treat worker as wedged, kill it so
                # the next call respawns cleanly.
                logger.warning("worker response timed out or stream closed")
                try:
                    self._proc.kill()
                except OSError:
                    pass
                self._proc = None
                return None

            try:
                response = json.loads(line)
            except json.JSONDecodeError as e:
                logger.warning("worker returned invalid JSON %r: %s", line, e)
                return None

            if "error" in response:
                logger.warning("worker reported error: %s", response["error"])
            return response

    @staticmethod
    def _read_line_with_timeout(stream, timeout_secs: float) -> str | None:
        """Read one line with a hard timeout.

        We spawn a helper thread to do the blocking readline and join with a
        timeout. That avoids the alternatives (select on a pipe FD, setting
        the pipe non-blocking) which are messier for text-mode Popen streams.
        """
        result: list[str | None] = [None]

        def _reader() -> None:
            try:
                result[0] = stream.readline()
            except Exception:
                result[0] = None

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout=timeout_secs)
        if t.is_alive():
            return None
        line = result[0]
        if not line:
            return None
        return line.strip()
