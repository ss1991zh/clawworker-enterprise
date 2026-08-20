"""只读数据库查询执行、取消和结果容量控制。"""

from __future__ import annotations

import json
import threading
from contextlib import closing

from host.db_connectors import safe_error


class QueryCancelled(RuntimeError):
    pass


class QueryTimedOut(RuntimeError):
    pass


class ResultTooLarge(RuntimeError):
    pass


class QueryExecutionSignal:
    """线程安全的查询中止信号；任务管理器可在驱动执行期间调用 cancel。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._cancel_driver = None
        self.reason = ""

    def bind(self, callback) -> None:
        with self._lock:
            self._cancel_driver = callback
            already_cancelled = self._event.is_set()
        if already_cancelled:
            self._invoke(callback)

    def clear_driver(self) -> None:
        with self._lock:
            self._cancel_driver = None

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if not self._event.is_set():
                self.reason = reason
                self._event.set()
            callback = self._cancel_driver
        if callback:
            self._invoke(callback)

    @staticmethod
    def _invoke(callback) -> None:
        try:
            callback()
        except Exception:
            pass

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            if self.reason == "timeout":
                raise QueryTimedOut("查询超过管理员设置的时间限制")
            raise QueryCancelled("查询已由用户取消")


def execute_readonly(*, connector, source, password: str, plan, signal: QueryExecutionSignal):
    """在驱动连接上执行已通过安全计划的 SQL。"""
    with closing(connector._connect(source, password)) as conn:
        cursor = conn.cursor()
        try:
            cancel_driver = getattr(cursor, "cancel", None)
            if not callable(cancel_driver):
                cancel_driver = getattr(conn, "cancel", None)
            if not callable(cancel_driver):
                cancel_driver = conn.close
            signal.bind(cancel_driver)
            if source.engine == "mysql":
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(f"SET SESSION MAX_EXECUTION_TIME={source.query_timeout_seconds * 1000}")
            elif source.engine == "postgresql":
                cursor.execute("SET default_transaction_read_only = on")
                cursor.execute("SET statement_timeout = %s", (source.query_timeout_seconds * 1000,))
            elif source.engine == "sqlserver":
                cursor.execute(f"SET LOCK_TIMEOUT {source.query_timeout_seconds * 1000}")
                try:
                    cursor.timeout = source.query_timeout_seconds
                except Exception:
                    pass
            signal.raise_if_cancelled()
            cursor.execute(plan.sql)
            signal.raise_if_cancelled()
            headers = [str(item[0]) for item in (cursor.description or [])]
            result_bytes = len(json.dumps(headers, ensure_ascii=False).encode("utf-8"))
            rows = []
            while len(rows) < plan.max_rows:
                signal.raise_if_cancelled()
                batch = cursor.fetchmany(min(256, plan.max_rows - len(rows)))
                if not batch:
                    break
                for raw in batch:
                    row = list(raw)
                    result_bytes += len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
                    if result_bytes > source.max_result_bytes:
                        raise ResultTooLarge(
                            f"查询结果超过管理员设置的 {source.max_result_bytes // 1048576} MB 限制"
                        )
                    rows.append(row)
            return headers, rows, result_bytes
        except (QueryCancelled, QueryTimedOut, ResultTooLarge):
            raise
        except Exception as exc:
            signal.raise_if_cancelled()
            detail = safe_error(exc).lower()
            if any(marker in detail for marker in (
                "timeout", "timed out", "statement timeout", "maximum statement execution",
                "query timeout", "hyt00", "hyt01",
            )):
                raise QueryTimedOut("查询超过管理员设置的时间限制") from exc
            raise
        finally:
            signal.clear_driver()
            try:
                cursor.close()
            except Exception:
                pass
