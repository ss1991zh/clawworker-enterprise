"""
A4 任务分发(architecture.md §A4)。

MVP 实现:LLM 响应直接经 HTTP 同步返回给客户端,不需要异步路由队列。
保留这个模块是为了后续扩展(WebSocket 长连接 + 异步任务队列)。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any


@dataclass
class TaskRecord:
    task_id: str
    username: str
    status: str  # "pending" | "running" | "done" | "failed"
    result: Any = None
    error: str = ""


class Dispatcher:
    """简化版任务分发:同步执行,内存记录任务元数据。"""

    def __init__(self):
        self._tasks: dict[str, TaskRecord] = {}

    def create(self, username: str) -> str:
        task_id = str(uuid.uuid4())
        self._tasks[task_id] = TaskRecord(task_id=task_id, username=username, status="pending")
        return task_id

    def mark_running(self, task_id: str) -> None:
        if task_id in self._tasks:
            self._tasks[task_id].status = "running"

    def complete(self, task_id: str, result: Any) -> None:
        if task_id in self._tasks:
            t = self._tasks[task_id]
            t.status = "done"
            t.result = result

    def fail(self, task_id: str, error: str) -> None:
        if task_id in self._tasks:
            t = self._tasks[task_id]
            t.status = "failed"
            t.error = error

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)
