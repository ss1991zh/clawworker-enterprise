from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from host.admin_ui import build_admin_router
from host.data_access import DataAccessStore
from host.data_access import redact_audit_sql
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


def test_admin_data_usage_page_filters_and_never_contains_result_rows(tmp_path):
    client, source, access = setup_admin(tmp_path)
    access.record_audit(
        request_id="req-1", username="alice", data_source_id=source.id,
        operation="query", sql_text="SELECT id FROM erp.orders LIMIT 10",
        tables=["erp.orders"], columns=["id"], status="success",
        row_count=2, duration_ms=15,
    )
    # 查询结果正文永远不进入 audit API，自然也不应出现在管理页面。
    secret_result_value = "CUSTOMER-SECRET-ROW-VALUE"

    page = client.get(
        f"/admin/data-usage?username=alice&data_source_id={source.id}&operation=query&status=success"
    )
    assert page.status_code == 200
    assert "数据使用记录" in page.text
    assert "SELECT id FROM erp.orders LIMIT 10" in page.text
    assert "erp.orders" in page.text
    assert "2 行" in page.text
    assert secret_result_value not in page.text
    assert access.audit_summary()["rows"] == 2


def test_admin_data_usage_filter_excludes_other_users(tmp_path):
    client, source, access = setup_admin(tmp_path)
    for username, sql in (("alice", "SELECT id FROM erp.orders"),
                          ("bob", "SELECT amount FROM erp.orders")):
        access.record_audit(
            request_id=f"req-{username}", username=username,
            data_source_id=source.id, operation="preview", sql_text=sql,
            tables=["erp.orders"], columns=["id"], status="success",
        )
    page = client.get("/admin/data-usage?username=alice")
    assert "SELECT id FROM erp.orders" in page.text
    assert "SELECT amount FROM erp.orders" not in page.text


def test_audit_sql_redacts_business_literals_but_keeps_query_shape(tmp_path):
    client, source, access = setup_admin(tmp_path)
    access.record_audit(
        request_id="req-sensitive", username="alice", data_source_id=source.id,
        operation="query",
        sql_text="SELECT id FROM erp.orders WHERE customer='张三' AND amount>10000 LIMIT 50",
        tables=["erp.orders"], columns=["id"], status="success", row_count=1,
    )
    row = access.list_audits(1)[0]
    assert "张三" not in row["sql_text"]
    assert "10000" not in row["sql_text"]
    assert "customer='***'" in row["sql_text"]
    assert "amount>?" in row["sql_text"]
    assert "LIMIT 50" in row["sql_text"]
    assert "张三" not in client.get("/admin/data-usage").text


def test_redact_audit_sql_handles_escaped_quotes():
    redacted = redact_audit_sql("SELECT id FROM t WHERE name='O''Brien' AND score=98.5")
    assert "O''Brien" not in redacted
    assert "98.5" not in redacted
    assert "name='***'" in redacted
