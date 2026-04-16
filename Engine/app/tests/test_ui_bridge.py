"""Tests for UIBridge — main-thread dispatch of background-thread callables."""
from __future__ import annotations

import threading
import time

from app.ui_bridge import UIBridge


class TestUIBridge:
    def test_empty_drain_returns_zero(self):
        bridge = UIBridge()
        assert bridge.drain() == 0

    def test_dispatched_callable_runs_on_drain(self):
        bridge = UIBridge()
        calls: list[int] = []
        bridge.dispatch(lambda: calls.append(1))
        assert calls == []  # not run yet
        count = bridge.drain()
        assert count == 1
        assert calls == [1]

    def test_multiple_callables_run_in_fifo_order(self):
        bridge = UIBridge()
        order: list[int] = []
        for i in range(5):
            bridge.dispatch(lambda i=i: order.append(i))
        assert bridge.drain() == 5
        assert order == [0, 1, 2, 3, 4]

    def test_exception_in_callable_does_not_stop_others(self):
        bridge = UIBridge()
        calls: list[str] = []

        def _boom():
            raise RuntimeError("user bug")

        bridge.dispatch(lambda: calls.append("before"))
        bridge.dispatch(_boom)
        bridge.dispatch(lambda: calls.append("after"))

        assert bridge.drain() == 3
        assert calls == ["before", "after"]

    def test_dispatch_is_thread_safe(self):
        bridge = UIBridge()
        recorded: list[int] = []

        def _producer(start: int) -> None:
            for i in range(start, start + 100):
                bridge.dispatch(lambda i=i: recorded.append(i))

        threads = [threading.Thread(target=_producer, args=(i * 100,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 4 producers × 100 each = 400 callables queued across threads.
        assert bridge.drain() == 400
        assert len(recorded) == 400
        # All 400 unique values across the 4 ranges should be present.
        assert sorted(recorded) == list(range(400))

    def test_drain_after_drain_is_empty(self):
        bridge = UIBridge()
        bridge.dispatch(lambda: None)
        assert bridge.drain() == 1
        assert bridge.drain() == 0

    def test_callable_can_re_dispatch(self):
        """A callable may enqueue a follow-up that runs in the same drain —
        this is desirable (no ~100ms wait for a follow-up UI update)."""
        bridge = UIBridge()
        calls: list[str] = []

        def _first():
            calls.append("first")
            bridge.dispatch(lambda: calls.append("second"))

        bridge.dispatch(_first)
        assert bridge.drain() == 2
        assert calls == ["first", "second"]
        assert bridge.drain() == 0
