"""数据库查询任务调度：并发/频率限制、取消、超时与一次性结果领取。"""
from __future__ import annotations

import secrets
import threading
import time
from collections import defaultdict, deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field

from host.data_access import DataAccessStore
from host.data_sources import DataSourceStore
from host.query_gateway import (
    QueryCancelled,
    QueryDenied,
    QueryExecutionSignal,
    QueryGateway,
    QueryTimedOut,
    ResultTooLarge,
)


TERMINAL = frozenset({"success", "failed", "cancelled", "timeout", "denied"})


class QueryControlDenied(ValueError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


@dataclass
class QueryTask:
    id: str
    request_id: str
    username: str
    data_source_id: str
    sql: str
    operation: str
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    error_code: str = ""
    row_count: int = 0
    result_bytes: int = 0
    result: dict | None = None
    signal: QueryExecutionSignal = field(default_factory=QueryExecutionSignal)
    future: Future | None = None


class QueryTaskManager:
    def __init__(self, gateway: QueryGateway, sources: DataSourceStore,
                 access: DataAccessStore, *, max_workers: int = 32,
                 result_ttl_seconds: int = 600) -> None:
        self.gateway, self.sources, self.access = gateway, sources, access
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="db-query")
        self._ttl = result_ttl_seconds
        self._tasks: dict[str, QueryTask] = {}
        self._recent: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = threading.RLock()

    def create(self, *, username: str, data_source_id: str, sql: str,
               operation: str = "query") -> dict:
        source = self.sources.get(data_source_id)
        if not source or not source.enabled:
            raise QueryControlDenied("数据源不存在或已停用", "source_unavailable")
        # 在占用任务槽位之前做完整权限/SQL 校验；后台执行时仍会再校验一次。
        try:
            self.gateway.preview(username=username, data_source_id=data_source_id,
                                 sql=sql, operation=operation)
        except QueryDenied as exc:
            self._audit_denied(username, data_source_id, operation, sql, exc.code)
            raise
        now = time.time()
        key = (username, data_source_id)
        with self._lock:
            self._cleanup_locked(now)
            active = sum(
                1 for task in self._tasks.values()
                if (task.username, task.data_source_id) == key and task.status not in TERMINAL
            )
            if active >= source.max_concurrent_queries:
                self._audit_denied(username, data_source_id, operation, sql, "concurrency_limit")
                raise QueryControlDenied(
                    f"当前已有 {active} 个查询在运行，请等待完成或先取消",
                    "concurrency_limit",
                )
            recent = self._recent[key]
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= source.max_queries_per_minute:
                self._audit_denied(username, data_source_id, operation, sql, "rate_limit")
                raise QueryControlDenied(
                    f"已达到每分钟 {source.max_queries_per_minute} 次查询限制，请稍后再试",
                    "rate_limit",
                )
            recent.append(now)
            task = QueryTask(
                id=secrets.token_hex(12), request_id=secrets.token_hex(10),
                username=username, data_source_id=data_source_id,
                sql=sql, operation=operation,
            )
            self._tasks[task.id] = task
            task.future = self._pool.submit(self._run, task)
            return self._public(task)

    def get(self, task_id: str, username: str) -> dict:
        with self._lock:
            task = self._owned(task_id, username)
            return self._public(task)

    def cancel(self, task_id: str, username: str) -> dict:
        with self._lock:
            task = self._owned(task_id, username)
            if task.status in TERMINAL:
                return self._public(task)
            task.signal.cancel("cancelled")
            if task.future and task.future.cancel():
                task.status, task.finished_at = "cancelled", time.time()
                self.access.record_audit(
                    request_id=task.request_id, username=task.username,
                    data_source_id=task.data_source_id, operation=task.operation,
                    sql_text=task.sql, tables=[], columns=[], status="cancelled",
                    error_code="cancelled_before_start",
                )
            return self._public(task)

    def consume_result(self, task_id: str, username: str) -> dict:
        with self._lock:
            task = self._owned(task_id, username)
            if task.status != "success" or task.result is None:
                raise QueryControlDenied("查询结果尚不可领取", "result_unavailable")
            result = task.result
            task.result = None
            result["data_source_id"] = task.data_source_id
            return result

    def _run(self, task: QueryTask) -> None:
        with self._lock:
            if task.status == "cancelled":
                return
            task.status, task.started_at = "running", time.time()
        source = self.sources.get(task.data_source_id)
        timer = threading.Timer(
            source.query_timeout_seconds,
            lambda: task.signal.cancel("timeout"),
        )
        timer.daemon = True
        timer.start()
        try:
            result = self.gateway.execute(
                username=task.username, data_source_id=task.data_source_id,
                sql=task.sql, operation=task.operation,
                request_id=task.request_id, signal=task.signal,
            )
            result["max_result_bytes"] = source.max_result_bytes
            with self._lock:
                task.result = result
                task.row_count = int(result.get("row_count", 0))
                task.result_bytes = int(result.get("result_bytes", 0))
                task.status = "success"
            expiry = threading.Timer(self._ttl, self._expire, args=(task.id,))
            expiry.daemon = True
            expiry.start()
        except QueryCancelled as exc:
            with self._lock:
                task.status, task.error, task.error_code = "cancelled", str(exc), "cancelled"
        except QueryTimedOut as exc:
            with self._lock:
                task.status, task.error, task.error_code = "timeout", str(exc), "query_timeout"
        except ResultTooLarge as exc:
            with self._lock:
                task.status, task.error, task.error_code = "denied", str(exc), "result_too_large"
        except Exception as exc:
            with self._lock:
                task.status, task.error, task.error_code = "failed", str(exc)[:500], type(exc).__name__
        finally:
            timer.cancel()
            with self._lock:
                task.finished_at = time.time()

    def _owned(self, task_id: str, username: str) -> QueryTask:
        self._cleanup_locked(time.time())
        task = self._tasks.get(task_id)
        if not task or task.username != username:
            raise QueryControlDenied("查询任务不存在", "task_not_found")
        return task

    def _cleanup_locked(self, now: float) -> None:
        expired = [task_id for task_id, task in self._tasks.items()
                   if task.finished_at and task.finished_at < now - self._ttl]
        for task_id in expired:
            del self._tasks[task_id]

    def _expire(self, task_id: str) -> None:
        """即使服务空闲且没有后续请求，也按时清掉管理端内存中的结果。"""
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.finished_at and task.finished_at <= time.time() - self._ttl:
                task.result = None
                del self._tasks[task_id]

    def _audit_denied(self, username: str, source_id: str, operation: str,
                      sql: str, code: str) -> None:
        self.access.record_audit(
            request_id=secrets.token_hex(10), username=username,
            data_source_id=source_id, operation=operation, sql_text=sql,
            tables=[], columns=[], status="denied", error_code=code,
        )

    @staticmethod
    def _public(task: QueryTask) -> dict:
        now = task.finished_at or time.time()
        base = task.started_at or task.created_at
        return {
            "task_id": task.id, "request_id": task.request_id,
            "status": task.status, "elapsed_ms": max(0, int((now - base) * 1000)),
            "row_count": task.row_count, "result_bytes": task.result_bytes,
            "error": task.error, "error_code": task.error_code,
            "can_cancel": task.status not in TERMINAL,
        }
