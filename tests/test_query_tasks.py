from __future__ import annotations

import threading
import time

import pytest

from host.data_access import DataAccessStore
from host.data_sources import CatalogColumn, DataSourceStore
from host.query_gateway import QueryCancelled, QueryDenied, QueryTimedOut, ResultTooLarge
from host.query_tasks import QueryControlDenied, QueryTaskManager


class ControlledGateway:
    def __init__(self, access=None):
        self.access = access
        self.started = threading.Event()
        self.release = threading.Event()
        self.mode = "success"

    def preview(self, **kwargs):
        if self.mode == "preview_denied":
            raise QueryDenied("字段未授权", "column_denied")
        return object()

    def execute(self, *, signal, request_id, data_source_id, **kwargs):
        self.started.set()
        try:
            while not self.release.wait(0.01):
                signal.raise_if_cancelled()
            signal.raise_if_cancelled()
            if self.mode == "large":
                raise ResultTooLarge("结果过大")
        except QueryCancelled:
            self._audit(request_id, data_source_id, "cancelled", "cancelled")
            raise
        except QueryTimedOut:
            self._audit(request_id, data_source_id, "timeout", "query_timeout")
            raise
        except ResultTooLarge:
            self._audit(request_id, data_source_id, "denied", "result_too_large")
            raise
        return {
            "request_id": request_id, "columns": ["id"], "rows": [[1]],
            "row_count": 1, "result_bytes": 12, "duration_ms": 10,
        }

    def _audit(self, request_id, source_id, status, code):
        if self.access:
            self.access.record_audit(
                request_id=request_id, username="alice", data_source_id=source_id,
                operation="query", sql_text="SELECT id FROM orders",
                tables=["orders"], columns=["id"], status=status, error_code=code,
            )


def setup_manager(tmp_path, **limits):
    db = tmp_path / "control.db"
    sources = DataSourceStore(
        db, protect=lambda value: "protected:" + value,
        unprotect=lambda value: value.removeprefix("protected:"),
        harden=lambda path: True, require_encryption=False,
    )
    source = sources.create(
        name="ERP", engine="mysql", host="db", port=3306,
        database_name="erp", username="reader", password="secret",
        ssl_mode="require", query_timeout_seconds=limits.get("timeout", 60),
        max_concurrent_queries=limits.get("concurrent", 1),
        max_queries_per_minute=limits.get("rate", 20),
        max_result_bytes=limits.get("bytes", 50 * 1024 * 1024),
    )
    sources.replace_catalog(source.id, [
        CatalogColumn("erp", "orders", "BASE TABLE", "id", "bigint", 1, False),
    ])
    access = DataAccessStore(db, harden=lambda path: True)
    gateway = ControlledGateway(access)
    manager = QueryTaskManager(gateway, sources, access, result_ttl_seconds=60)
    return manager, gateway, source, access


def wait_terminal(manager, task_id, username="alice", timeout=3):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = manager.get(task_id, username)
        if task["status"] in {"success", "failed", "cancelled", "timeout", "denied"}:
            return task
        time.sleep(0.01)
    raise AssertionError("query task did not finish")


def test_query_task_can_be_cancelled_and_is_audited(tmp_path):
    manager, gateway, source, access = setup_manager(tmp_path)
    task = manager.create(username="alice", data_source_id=source.id,
                          sql="SELECT id FROM orders")
    assert gateway.started.wait(1)
    manager.cancel(task["task_id"], "alice")
    status = wait_terminal(manager, task["task_id"])
    assert status["status"] == "cancelled"
    assert access.list_audits(1)[0]["status"] == "cancelled"


def test_per_user_concurrency_limit_blocks_second_active_query(tmp_path):
    manager, gateway, source, access = setup_manager(tmp_path, concurrent=1)
    first = manager.create(username="alice", data_source_id=source.id,
                           sql="SELECT id FROM orders")
    assert gateway.started.wait(1)
    with pytest.raises(QueryControlDenied) as error:
        manager.create(username="alice", data_source_id=source.id,
                       sql="SELECT id FROM orders")
    assert error.value.code == "concurrency_limit"
    assert access.list_audits(1)[0]["error_code"] == "concurrency_limit"
    manager.cancel(first["task_id"], "alice")


def test_rate_limit_blocks_burst_even_after_queries_finish(tmp_path):
    manager, gateway, source, access = setup_manager(tmp_path, rate=1)
    gateway.release.set()
    first = manager.create(username="alice", data_source_id=source.id,
                           sql="SELECT id FROM orders")
    assert wait_terminal(manager, first["task_id"])["status"] == "success"
    with pytest.raises(QueryControlDenied) as error:
        manager.create(username="alice", data_source_id=source.id,
                       sql="SELECT id FROM orders")
    assert error.value.code == "rate_limit"


def test_result_is_consumed_once_and_not_exposed_in_status(tmp_path):
    manager, gateway, source, _ = setup_manager(tmp_path)
    gateway.release.set()
    task = manager.create(username="alice", data_source_id=source.id,
                          sql="SELECT id FROM orders")
    status = wait_terminal(manager, task["task_id"])
    assert status["status"] == "success"
    assert "rows" not in status
    result = manager.consume_result(task["task_id"], "alice")
    assert result["rows"] == [[1]]
    assert result["data_source_id"] == source.id
    with pytest.raises(QueryControlDenied):
        manager.consume_result(task["task_id"], "alice")


def test_result_too_large_has_distinct_terminal_status(tmp_path):
    manager, gateway, source, _ = setup_manager(tmp_path)
    gateway.mode = "large"
    gateway.release.set()
    task = manager.create(username="alice", data_source_id=source.id,
                          sql="SELECT id FROM orders")
    status = wait_terminal(manager, task["task_id"])
    assert status["status"] == "denied"
    assert status["error_code"] == "result_too_large"


def test_task_is_visible_only_to_its_owner(tmp_path):
    manager, gateway, source, _ = setup_manager(tmp_path)
    task = manager.create(username="alice", data_source_id=source.id,
                          sql="SELECT id FROM orders")
    with pytest.raises(QueryControlDenied) as error:
        manager.get(task["task_id"], "bob")
    assert error.value.code == "task_not_found"
    manager.cancel(task["task_id"], "alice")


def test_wall_clock_timeout_cancels_driver_and_has_distinct_audit(tmp_path):
    manager, gateway, source, access = setup_manager(tmp_path, timeout=1)
    task = manager.create(username="alice", data_source_id=source.id,
                          sql="SELECT id FROM orders")
    status = wait_terminal(manager, task["task_id"], timeout=2.5)
    assert status["status"] == "timeout"
    assert status["error_code"] == "query_timeout"
    audit = access.list_audits(1)[0]
    assert audit["status"] == "timeout"


def test_preflight_permission_denial_is_also_audited(tmp_path):
    manager, gateway, source, access = setup_manager(tmp_path)
    gateway.mode = "preview_denied"
    with pytest.raises(QueryDenied):
        manager.create(username="alice", data_source_id=source.id,
                       sql="SELECT secret FROM orders")
    audit = access.list_audits(1)[0]
    assert audit["status"] == "denied"
    assert audit["error_code"] == "column_denied"


def test_success_result_expires_without_needing_another_api_call(tmp_path):
    manager, gateway, source, _ = setup_manager(tmp_path)
    manager._ttl = 0.05
    gateway.release.set()
    task = manager.create(username="alice", data_source_id=source.id,
                          sql="SELECT id FROM orders")
    assert wait_terminal(manager, task["task_id"])["status"] == "success"
    time.sleep(0.12)
    with pytest.raises(QueryControlDenied) as error:
        manager.get(task["task_id"], "alice")
    assert error.value.code == "task_not_found"

