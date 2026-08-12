from __future__ import annotations

from dataclasses import replace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from host.admin_ui import build_admin_router
from host.data_sources import DataSourceStore
from host.db_connectors import ConnectionTestResult


class StubConnectors:
    def test(self, source, password):
        assert password == "db-secret"
        return ConnectionTestResult(True, "连接成功 · 只读", "8.4.10")

    def inspect_catalog(self, source, password):
        from host.data_sources import CatalogColumn
        assert password == "db-secret"
        return [CatalogColumn("erp", "orders", "BASE TABLE", "id", "bigint", 1, False)]


class Empty:
    def list_all(self): return []


def _app(tmp_path):
    store = DataSourceStore(
        tmp_path / "control.db",
        protect=lambda value: "dpapi:test:" + value[::-1],
        unprotect=lambda value: value.removeprefix("dpapi:test:")[::-1],
        harden=lambda path: True,
        require_encryption=True,
    )
    app = FastAPI()
    app.include_router(build_admin_router(
        auth_manager=Empty(), user_manager=Empty(), dispatcher=Empty(),
        llm_config_store=Empty(), provider_manager=Empty(), call_stats=Empty(),
        data_source_store=store, connector_registry=StubConnectors(),
    ))
    return app, store


def test_data_source_page_and_end_to_end_admin_actions(tmp_path):
    app, store = _app(tmp_path)
    client = TestClient(app)

    page = client.get("/admin/data-sources")
    assert page.status_code == 200
    assert "企业数据源" in page.text

    created = client.post("/admin/data-sources", data={
        "name": "ERP库", "engine": "mysql", "host": "db.internal", "port": "3306",
        "database_name": "erp", "username": "reader", "password": "db-secret",
        "ssl_mode": "require", "connect_timeout_seconds": "8",
        "query_timeout_seconds": "60", "max_rows": "10000",
        "max_result_mb": "25", "max_concurrent_queries": "2",
        "max_queries_per_minute": "12",
    }, follow_redirects=False)
    assert created.status_code == 303
    source = store.list_all()[0]
    assert source.max_result_bytes == 25 * 1024 * 1024
    assert source.max_concurrent_queries == 2
    assert source.max_queries_per_minute == 12

    tested = client.post(f"/admin/data-sources/{source.id}/test", follow_redirects=False)
    assert tested.status_code == 303
    assert store.get(source.id).last_test_status == "ok"

    synced = client.post(f"/admin/data-sources/{source.id}/sync", follow_redirects=False)
    assert synced.status_code == 303
    assert store.catalog_summary(source.id)["columns"] == 1

    html = client.get("/admin/data-sources").text
    assert "db-secret" not in html
    assert "ERP库" in html

