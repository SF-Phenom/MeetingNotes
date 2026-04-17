"""Call detector worker process.

Runs as a long-lived subprocess so osascript / psutil calls never happen
inside the main menubar process. This matters because the main process
hosts MLX/GPU threads for Parakeet, and fork() while MLX holds malloc locks
corrupts the heap in the child.

Spawned by :class:`app.call_detector_proxy.CallDetectorProxy`.

Protocol — line-delimited JSON on stdin/stdout:

    stdin (one JSON object per line):
        {"cmd": "detect"}
        {"cmd": "alive", "source": "zoom"}
        {"cmd": "ping"}

    stdout (one JSON object per line):
        {"result": {...}|null}
        {"alive": true|false}
        {"pong": true}
        {"error": "message"}
"""
from __future__ import annotations

import json
import logging
import os
import sys


# Keep the package path setup identical to menubar.py so `from app import ...`
# works when the worker is launched directly (via `python <path>`).
_BASE_DIR = os.environ.get(
    "MEETINGNOTES_HOME", os.path.expanduser("~/MeetingNotes")
)
_ENGINE_DIR = os.path.join(_BASE_DIR, "Engine")
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from app import call_detector  # noqa: E402

logger = logging.getLogger(__name__)


def _handle(cmd: dict) -> dict:
    op = cmd.get("cmd")
    if op == "detect":
        return {"result": call_detector.detect_active_call()}
    if op == "alive":
        source = cmd.get("source", "")
        return {"alive": call_detector.is_call_still_active(source)}
    if op == "ping":
        return {"pong": True}
    return {"error": "unknown command: {}".format(op)}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s worker: %(message)s",
        stream=sys.stderr,
    )
    logger.info("call detector worker started (pid=%d)", os.getpid())

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError as e:
            response: dict = {"error": "invalid JSON: {}".format(e)}
        else:
            try:
                response = _handle(cmd)
            except Exception as exc:  # noqa: BLE001 — surface any handler crash
                logger.exception("handler error: %s", exc)
                response = {"error": str(exc)}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    logger.info("stdin closed — exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
