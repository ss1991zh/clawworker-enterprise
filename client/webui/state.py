"""用户端进程内状态与原子 JSON 持久化。"""
from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, MutableMapping
from typing import Any

from shared.storage import atomic_write_json


class ThreadSafeState(MutableMapping[str, Any]):
    """保留 dict 调用方式，但让登录/退出与后台任务读取不相互撕裂。"""

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._data = dict(initial or {})
        self._lock = threading.RLock()

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        with self._lock:
            self._data[key] = value

    def __delitem__(self, key: str) -> None:
        with self._lock:
            del self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.snapshot())

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._data.get(key, default)

    def update(self, *args, **kwargs) -> None:
        incoming = dict(*args, **kwargs)
        with self._lock:
            self._data.update(incoming)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._data)
