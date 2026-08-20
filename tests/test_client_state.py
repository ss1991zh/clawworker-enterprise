from __future__ import annotations

import json
import threading

from client.webui.state import ThreadSafeState, atomic_write_json


def test_thread_safe_state_update_is_visible_as_one_snapshot():
    state = ThreadSafeState({"host_url": "old", "token": "old"})
    ready = threading.Event()

    def replace():
        state.update({"host_url": "new", "token": "new"})
        ready.set()

    thread = threading.Thread(target=replace)
    thread.start()
    assert ready.wait(1)
    thread.join()

    assert state.snapshot() == {"host_url": "new", "token": "new"}


def test_atomic_write_json_replaces_complete_document(tmp_path):
    path = tmp_path / "client-config.json"
    atomic_write_json(path, {"host_url": "https://old:8443", "backend": "real"})
    atomic_write_json(path, {"host_url": "https://new:8443", "backend": "real"})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "host_url": "https://new:8443",
        "backend": "real",
    }
    assert list(tmp_path.glob("*.tmp")) == []
