from __future__ import annotations

from client.webui.scheduler import Scheduler
from host.runtime import HostRuntime, LazyComponent


def test_lazy_component_initializes_once():
    calls = []
    component = LazyComponent(lambda: calls.append("created") or {"ok": True}, "test")

    assert component.initialized is False
    assert component.get() == {"ok": True}
    assert component.get() == {"ok": True}
    assert component.initialized is True
    assert calls == ["created"]


def test_host_runtime_constructor_has_no_persistent_component_side_effects():
    runtime = HostRuntime()

    assert runtime.auth_manager.initialized is False
    assert runtime.admin_auth.initialized is False
    assert runtime.data_source_store.initialized is False
    assert runtime.data_access_store.initialized is False
    assert runtime.query_task_manager.initialized is False
    assert runtime.readiness()["status"] == "starting"
    assert runtime.readiness()["pending_components"] > 0


def test_scheduler_can_stop_and_restart_cleanly():
    class _Tasks:
        def all_enabled(self):
            return []

    scheduler = Scheduler(_Tasks(), lambda _task: None, poll_seconds=1)

    scheduler.start()
    assert scheduler._thread is not None and scheduler._thread.is_alive()
    scheduler.stop()
    assert scheduler._thread is None
    scheduler.start()
    assert scheduler._thread is not None and scheduler._thread.is_alive()
    scheduler.stop()
