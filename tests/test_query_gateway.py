from __future__ import annotations

import pytest

from host.data_access import DataAccessStore
from host.data_sources import CatalogColumn, DataSourceStore
from host.db_connectors import ConnectorRegistry
from host.query_gateway import QueryDenied, QueryGateway


def setup_gateway(tmp_path):
    db = tmp_path / "control.db"
    sources = DataSourceStore(
        db, protect=lambda v: "dpapi:test:" + v[::-1],
        unprotect=lambda v: v.removeprefix("dpapi:test:")[::-1],
        harden=lambda path: True, require_encryption=True,
    )
    source = sources.create(
        name="ERP", engine="mysql", host="db", port=3306, database_name="erp",
        username="reader", password="secret", ssl_mode="require",
    )
    sources.replace_catalog(source.id, [
        CatalogColumn("erp", "orders", "BASE TABLE", "id", "bigint", 1, False),
        CatalogColumn("erp", "orders", "BASE TABLE", "amount", "decimal", 2, False),
        CatalogColumn("erp", "orders", "BASE TABLE", "secret_note", "varchar", 3, True),
        CatalogColumn("erp", "customers", "BASE TABLE", "id", "bigint", 1, False),
        CatalogColumn("erp", "customers", "BASE TABLE", "name", "varchar", 2, False),
    ])
    access = DataAccessStore(db, harden=lambda path: True)
    access.grant(username="alice", data_source_id=source.id, schema_name="erp",
                 table_name="orders", allowed_columns=["id", "amount"],
                 operations=["browse", "query", "analyze"], max_rows=500)
    return QueryGateway(sources, access, ConnectorRegistry()), source, access


def test_authorized_select_is_limited(tmp_path):
    gateway, source, _ = setup_gateway(tmp_path)
    plan = gateway.plan(username="alice", data_source_id=source.id,
                        sql="SELECT id, amount FROM orders", operation="query")
    assert plan.max_rows == 500
    assert "LIMIT 500" in plan.sql.upper()


@pytest.mark.parametrize("sql,code", [
    ("SELECT * FROM orders", "star_denied"),
    ("SELECT secret_note FROM orders", "column_denied"),
    ("SELECT name FROM customers", "table_denied"),
    ("DELETE FROM orders", "not_select"),
    ("SELECT id FROM orders; SELECT id FROM customers", "multiple_statements"),
    ("SELECT load_file('/etc/passwd') FROM orders", "dangerous_function"),
])
def test_unsafe_or_unauthorized_queries_denied(tmp_path, sql, code):
    gateway, source, _ = setup_gateway(tmp_path)
    with pytest.raises(QueryDenied) as err:
        gateway.plan(username="alice", data_source_id=source.id, sql=sql)
    assert err.value.code == code


def test_no_permission_is_default_deny(tmp_path):
    gateway, source, _ = setup_gateway(tmp_path)
    with pytest.raises(QueryDenied) as err:
        gateway.plan(username="bob", data_source_id=source.id,
                     sql="SELECT id FROM orders")
    assert err.value.code == "no_source_permission"


def test_revocation_takes_effect_immediately(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    policy = access.list_for_user("alice")[0]
    access.revoke(policy.id)
    with pytest.raises(QueryDenied):
        gateway.plan(username="alice", data_source_id=source.id,
                     sql="SELECT id FROM orders")


def test_denied_execution_is_audited(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    with pytest.raises(QueryDenied):
        gateway.execute(username="alice", data_source_id=source.id,
                        sql="SELECT secret_note FROM orders")
    audits = access.list_audits()
    assert audits[0]["status"] == "denied"
    assert audits[0]["error_code"] == "column_denied"


def test_same_table_name_in_two_schemas_requires_explicit_schema(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    access.grant(username="alice", data_source_id=source.id, schema_name="archive",
                 table_name="orders", allowed_columns=["id"], operations=["query"])
    with pytest.raises(QueryDenied) as err:
        gateway.plan(username="alice", data_source_id=source.id, sql="SELECT id FROM orders")
    assert err.value.code == "ambiguous_schema"

    plan = gateway.plan(username="alice", data_source_id=source.id,
                        sql="SELECT id FROM erp.orders")
    assert "erp.orders" in plan.sql.lower()
