from __future__ import annotations

import threading
import time

import pytest

from client.webui.background_tasks import BackgroundTaskManager, BackgroundTaskRejected


def test_background_tasks_are_bounded_and_recover_capacity():
    manager = BackgroundTaskManager(max_workers=1, max_pending=2)
    release = threading.Event()
    started = threading.Event()

    def blocking():
        started.set()
        release.wait(2)

    manager.submit("first", blocking)
    assert started.wait(1)
    manager.submit("second", blocking)
    with pytest.raises(BackgroundTaskRejected):
        manager.submit("overflow", blocking)

    snapshot = manager.snapshot()
    assert snapshot == {"active": 1, "queued": 1, "capacity": 2}
    release.set()
    deadline = time.monotonic() + 2
    while manager.snapshot()["active"] or manager.snapshot()["queued"]:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    manager.close()


def test_background_manager_can_restart_after_lifespan_close():
    manager = BackgroundTaskManager(max_workers=1, max_pending=1)
    finished = threading.Event()
    manager.close()
    manager.submit("restart", finished.set)
    assert finished.wait(1)
    manager.close()
