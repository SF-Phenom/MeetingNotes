"""Main-thread dispatch for rumps/AppKit.

rumps and the underlying AppKit are not thread-safe: any menu mutation,
rumps.notification, or rumps.alert call from a background thread can crash
the process or wedge the UI. The bridge marshals callables onto the main
rumps thread via a thread-safe queue that is drained from a @rumps.timer.

Usage:

    # Wire once during menubar init:
    self._ui_bridge = UIBridge()

    @rumps.timer(0.1)
    def _ui_drain_tick(self, _sender):
        self._ui_bridge.drain()

    # From any background thread:
    self._ui_bridge.dispatch(lambda: rumps.notification(...))
"""
from __future__ import annotations

import logging
import queue
from typing import Callable

logger = logging.getLogger(__name__)


class UIBridge:
    """Thread-safe queue of zero-arg callables drained on the main thread."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue()

    def dispatch(self, fn: Callable[[], None]) -> None:
        """Schedule ``fn`` to run on the next drain tick. Non-blocking."""
        self._queue.put(fn)

    def drain(self) -> int:
        """Run every pending callable. Returns the number executed.

        Exceptions raised by individual callables are logged and swallowed
        so one misbehaving task never stops subsequent ones from running.
        Intended to be called only from the main rumps thread.
        """
        count = 0
        while True:
            try:
                fn = self._queue.get_nowait()
            except queue.Empty:
                return count
            count += 1
            try:
                fn()
            except Exception as e:  # noqa: BLE001 — isolate faulty callables
                logger.error("UI bridge callable raised: %s", e, exc_info=True)
