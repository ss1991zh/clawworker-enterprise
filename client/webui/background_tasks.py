"""用户端有界后台任务执行器：限制低配置电脑上的线程与排队数量。"""
from __future__ import annotations

import secrets
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class BackgroundTaskRejected(RuntimeError):
    pass


class BackgroundTaskManager:
    def __init__(self, *, max_workers: int = 4, max_pending: int = 24) -> None:
        if max_workers < 1 or max_pending < max_workers:
            raise ValueError("max_pending 必须不少于 max_workers")
        self._max_workers = max_workers
        self._max_pending = max_pending
        self._lock = threading.RLock()
        self._pool: ThreadPoolExecutor | None = None
        self._slots: threading.BoundedSemaphore | None = None
        self._futures: dict[str, Future] = {}

    def start(self) -> None:
        with self._lock:
            if self._pool is not None:
                return
            self._pool = ThreadPoolExecutor(
                max_workers=self._max_workers,
                thread_name_prefix="client-job",
            )
            self._slots = threading.BoundedSemaphore(self._max_pending)

    def submit(self, name: str, fn: Callable[..., Any], /, *args, **kwargs) -> str:
        self.start()
        with self._lock:
            pool, slots = self._pool, self._slots
            if pool is None or slots is None or not slots.acquire(blocking=False):
                raise BackgroundTaskRejected(
                    f"后台任务已达到上限（最多排队 {self._max_pending} 个），请稍后重试"
                )
            task_id = f"{name[:40]}-{secrets.token_hex(6)}"

            def run() -> Any:
                try:
                    return fn(*args, **kwargs)
                finally:
                    slots.release()

            try:
                future = pool.submit(run)
            except Exception:
                slots.release()
                raise
            self._futures[task_id] = future
            future.add_done_callback(lambda _future: self._forget(task_id))
            return task_id

    def _forget(self, task_id: str) -> None:
        with self._lock:
            self._futures.pop(task_id, None)

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            running = sum(1 for future in self._futures.values() if future.running())
            return {"active": running, "queued": len(self._futures) - running,
                    "capacity": self._max_pending}

    def close(self) -> None:
        with self._lock:
            pool = self._pool
            self._pool = None
            self._slots = None
            futures = list(self._futures.values())
            self._futures.clear()
        for future in futures:
            future.cancel()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
