from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from host.data_access import DataAccessStore
from host.data_sources import CatalogColumn, DataSourceStore
from host.db_connectors import ConnectorRegistry
from host.query_gateway import QueryGateway


def _install_catalog_stores(tmp_path, monkeypatch):
    db = tmp_path / "control.db"
    # host.server 在导入时会初始化控制库，测试必须先把默认位置隔离到 tmp_path。
    monkeypatch.setenv("CLAWWORKER_CONTROL_DB", str(db))
    server = importlib.import_module("host.server")
    sources = DataSourceStore(
        db, protect=lambda value: "protected:" + value,
        unprotect=lambda value: value.removeprefix("protected:"),
        harden=lambda path: True, require_encryption=False,
    )
    source = sources.create(
        name="ERP", engine="mysql", host="db", port=3306,
        database_name="erp", username="reader", password="secret",
        ssl_mode="require",
    )
    sources.replace_catalog(source.id, [
        CatalogColumn("erp", "orders", "BASE TABLE", "id", "bigint", 1, False),
        CatalogColumn("erp", "orders", "BASE TABLE", "amount", "decimal", 2, False),
        CatalogColumn("erp", "customers", "BASE TABLE", "name", "varchar", 1, False),
    ])
    access = DataAccessStore(db, harden=lambda path: True)
    gateway = QueryGateway(sources, access, ConnectorRegistry())
    monkeypatch.setattr(server, "data_source_store", sources)
    monkeypatch.setattr(server, "data_access_store", access)
    monkeypatch.setattr(server, "query_gateway", gateway)
    return server, source, access


def test_catalog_shows_only_tables_and_columns_with_explicit_browse_permission(
    tmp_path, monkeypatch,
):
    server, source, access = _install_catalog_stores(tmp_path, monkeypatch)
    access.grant(
        username="alice", data_source_id=source.id, schema_name="erp",
        table_name="orders", allowed_columns=["id"], operations=["browse"],
    )
    # amount 没有包含在浏览字段中；customers 只有查询权限。两者都不能出现在目录接口。
    access.grant(
        username="alice", data_source_id=source.id, schema_name="erp",
        table_name="customers", allowed_columns=["name"], operations=["query"],
    )

    catalog = server.authorized_catalog(
        source.id, sess=SimpleNamespace(username="alice"),
    )

    assert [(item["table"], [c["name"] for c in item["columns"]])
            for item in catalog] == [("orders", ["id"])]


def test_query_only_user_cannot_open_catalog(tmp_path, monkeypatch):
    server, source, access = _install_catalog_stores(tmp_path, monkeypatch)
    access.grant(
        username="bob", data_source_id=source.id, schema_name="erp",
        table_name="orders", allowed_columns=["id"], operations=["query"],
    )

    with pytest.raises(HTTPException) as error:
        server.authorized_catalog(source.id, sess=SimpleNamespace(username="bob"))
    assert error.value.status_code == 403
    audit = access.list_audits(1)[0]
    assert audit["operation"] == "browse"
    assert audit["status"] == "denied"

