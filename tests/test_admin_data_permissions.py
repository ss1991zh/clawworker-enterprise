from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from host.admin_ui import build_admin_router
from host.data_access import DataAccessStore
from host.data_sources import CatalogColumn, DataSourceStore
from host.db_connectors import ConnectorRegistry
from host.query_gateway import QueryGateway


class Empty:
    def list_all(self):
        return []


class Users(Empty):
    def __init__(self):
        self._accounts = {"alice": object(), "bob": object()}


def setup_admin(tmp_path):
    db = tmp_path / "control.db"
    sources = DataSourceStore(
        db, protect=lambda value: "dpapi:test:" + value[::-1],
        unprotect=lambda value: value.removeprefix("dpapi:test:")[::-1],
        harden=lambda path: True, require_encryption=True,
    )
    source = sources.create(
        name="ERP", engine="mysql", host="db", port=3306, database_name="erp",
        username="reader", password="secret", ssl_mode="require",
    )
    sources.replace_catalog(source.id, [
        CatalogColumn("erp", "orders", "BASE TABLE", "id", "bigint", 1, False),
        CatalogColumn("erp", "orders", "BASE TABLE", "region", "varchar", 2, False),
        CatalogColumn("erp", "orders", "BASE TABLE", "amount", "decimal", 3, False),
    ])
    access = DataAccessStore(db, harden=lambda path: True)
    gateway = QueryGateway(sources, access, ConnectorRegistry())
    app = FastAPI()
    app.include_router(build_admin_router(
        auth_manager=Empty(), user_manager=Users(), dispatcher=Empty(),
        llm_config_store=Empty(), provider_manager=Empty(), call_stats=Empty(),
        data_source_store=sources, connector_registry=ConnectorRegistry(),
        data_access_store=access, query_gateway=gateway,
    ))
    return TestClient(app), source, access


def test_admin_can_create_group_add_member_and_grant_scoped_masked_access(tmp_path):
    client, source, access = setup_admin(tmp_path)

    created = client.post(
        "/admin/data-permissions/groups",
        data={"name": "华东销售", "description": "只访问华东订单"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    group = access.list_groups()[0]

    member = client.post(
        f"/admin/data-permissions/groups/{group.id}/members",
        data={"username": "bob"}, follow_redirects=False,
    )
    assert member.status_code == 303
    assert access.get_group(group.id).members == ("bob",)

    granted = client.post(
        "/admin/data-permissions",
        data={
            "subject": f"group:{group.id}", "data_source_id": source.id,
            "schema_name": "erp", "table_name": "orders",
            "allowed_columns": "id,region,amount",
            "operations": ["browse", "query"], "max_rows": "500",
            "row_filter_sql": "region = '华东'", "masked_columns": "amount:partial",
        },
        follow_redirects=False,
    )
    assert granted.status_code == 303
    policy = access.list_for_user("bob", source.id)[0]
    assert policy.row_filter_sql == "region = '华东'"
    assert policy.mask_for("amount") == "partial"

    page = client.get("/admin/data-permissions")
    assert page.status_code == 200
    assert "华东销售" in page.text
    assert "region = &#39;华东&#39;" in page.text
    assert "部分隐藏" in page.text


def test_admin_rejects_invalid_row_filter_before_saving(tmp_path):
    client, source, access = setup_admin(tmp_path)
    response = client.post(
        "/admin/data-permissions",
        data={
            "subject": "user:alice", "data_source_id": source.id,
            "schema_name": "erp", "table_name": "orders",
            "allowed_columns": "id", "operations": ["query"],
            "row_filter_sql": "unknown_column = 1", "masked_columns": "",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert access.list_for_user("alice", source.id) == []
