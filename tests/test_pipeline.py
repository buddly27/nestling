"""Unit tests for Pipeline fan-out."""

import asyncio
from datetime import datetime

import numpy as np
import pytest

from nestling.camera import Frame, Pipeline


def _frame() -> Frame:
    return Frame(at=datetime.now(), img=np.zeros((10, 10, 3), dtype=np.uint8))


class TestPipeline:
    def test_subscribe_returns_queue(self):
        p = Pipeline()
        q = p.subscribe()
        assert isinstance(q, asyncio.Queue)

    def test_broadcast_delivers_to_subscriber(self):
        p = Pipeline()
        q = p.subscribe(maxsize=4)
        f = _frame()
        p.broadcast(f)
        assert q.get_nowait() is f

    def test_broadcast_delivers_to_multiple_subscribers(self):
        p = Pipeline()
        q1 = p.subscribe(maxsize=4)
        q2 = p.subscribe(maxsize=4)
        f = _frame()
        p.broadcast(f)
        assert q1.get_nowait() is f
        assert q2.get_nowait() is f

    def test_broadcast_drops_when_queue_full(self):
        p = Pipeline()
        q = p.subscribe(maxsize=1)
        f1, f2 = _frame(), _frame()
        p.broadcast(f1)
        p.broadcast(f2)  # queue full — should drop silently
        assert q.qsize() == 1
        assert q.get_nowait() is f1

    def test_unsubscribe_removes_queue(self):
        p = Pipeline()
        q = p.subscribe(maxsize=4)
        p.unsubscribe(q)
        p.broadcast(_frame())
        assert q.empty()

    def test_unsubscribe_nonexistent_is_safe(self):
        p = Pipeline()
        q = p.subscribe(maxsize=4)
        p.unsubscribe(q)
        p.unsubscribe(q)  # second call must not raise

    def test_close_puts_none_sentinel(self):
        p = Pipeline()
        q = p.subscribe(maxsize=4)
        p.close()
        assert q.get_nowait() is None

    def test_close_clears_subscriber_list(self):
        p = Pipeline()
        p.subscribe()
        p.close()
        f = _frame()
        p.broadcast(f)  # should not raise even with no subscribers

    def test_close_on_full_queue_still_delivers_sentinel(self):
        p = Pipeline()
        q = p.subscribe(maxsize=1)
        p.broadcast(_frame())  # fill the queue
        # close() evicts the queued frame to make room for the sentinel —
        # the sentinel MUST get through so the MJPEG handler can exit cleanly.
        p.close()
        assert q.get_nowait() is None
