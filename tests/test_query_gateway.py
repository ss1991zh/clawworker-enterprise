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


def test_group_permission_is_inherited_and_revoked_immediately(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    group = access.create_group("华东销售", "只看华东订单")
    access.add_group_member(group.id, "bob")
    policy = access.grant_group(
        group_id=group.id, data_source_id=source.id, schema_name="erp",
        table_name="orders", allowed_columns=["id", "amount"],
        operations=["query"], max_rows=120, row_filter_sql="region = '华东'",
    )

    plan = gateway.plan(username="bob", data_source_id=source.id,
                        sql="SELECT id, amount FROM orders")
    assert plan.max_rows == 120
    assert "orders.region = '华东'" in plan.sql

    access.revoke_group_policy(policy.id)
    with pytest.raises(QueryDenied) as err:
        gateway.plan(username="bob", data_source_id=source.id,
                     sql="SELECT id FROM orders")
    assert err.value.code == "no_source_permission"


def test_multiple_group_row_ranges_are_combined_as_union(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    # 移除 alice 的无行限制个人授权，避免它正确地放开全部行。
    access.revoke(access.list_for_user("alice", source.id)[0].id)
    for name, region in (("华东组", "华东"), ("华南组", "华南")):
        group = access.create_group(name)
        access.add_group_member(group.id, "alice")
        access.grant_group(
            group_id=group.id, data_source_id=source.id, schema_name="erp",
            table_name="orders", allowed_columns=["id"], operations=["query"],
            row_filter_sql=f"region = '{region}'",
        )

    plan = gateway.plan(username="alice", data_source_id=source.id,
                        sql="SELECT id FROM orders o")
    assert "o.region = '华东'" in plan.sql
    assert "o.region = '华南'" in plan.sql
    assert " OR " in plan.sql.upper()


def test_columns_from_different_scopes_cannot_be_combined(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    access.revoke(access.list_for_user("alice", source.id)[0].id)
    for name, column, region in (
        ("编号组", "id", "华东"),
        ("金额组", "amount", "华南"),
    ):
        group = access.create_group(name)
        access.add_group_member(group.id, "alice")
        access.grant_group(
            group_id=group.id, data_source_id=source.id, schema_name="erp",
            table_name="orders", allowed_columns=[column], operations=["query"],
            row_filter_sql=f"region = '{region}'",
        )
    with pytest.raises(QueryDenied) as err:
        gateway.plan(username="alice", data_source_id=source.id,
                     sql="SELECT id, amount FROM orders")
    assert err.value.code == "column_scope_conflict"


@pytest.mark.parametrize("strategy,marker", [
    ("partial", "***"),
    ("hash", "SHA2"),
    ("null", "NULL AS amount"),
])
def test_group_policy_masks_result_columns(tmp_path, strategy, marker):
    gateway, source, access = setup_gateway(tmp_path)
    group = access.create_group("受限财务")
    access.add_group_member(group.id, "bob")
    access.grant_group(
        group_id=group.id, data_source_id=source.id, schema_name="erp",
        table_name="orders", allowed_columns=["id", "amount"], operations=["query"],
        masked_columns={"amount": strategy},
    )
    plan = gateway.plan(username="bob", data_source_id=source.id,
                        sql="SELECT id, amount FROM orders")
    assert marker.upper() in plan.sql.upper()
    assert " AS amount" in plan.sql


def test_unmasked_direct_permission_overrides_group_mask(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    group = access.create_group("受限财务")
    access.add_group_member(group.id, "alice")
    access.grant_group(
        group_id=group.id, data_source_id=source.id, schema_name="erp",
        table_name="orders", allowed_columns=["amount"], operations=["query"],
        masked_columns={"amount": "hash"},
    )
    plan = gateway.plan(username="alice", data_source_id=source.id,
                        sql="SELECT amount FROM orders")
    assert "SHA2" not in plan.sql.upper()


def test_unqualified_column_across_tables_uses_stricter_mask(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    access.grant(
        username="alice", data_source_id=source.id, schema_name="erp",
        table_name="customers", allowed_columns=["id"], operations=["query"],
        masked_columns={"id": "hash"},
    )
    # SQL 本身会因同名列产生歧义，但网关仍必须先选择最严格策略，绝不能让
    # orders.id 的原值授权覆盖 customers.id 的脱敏要求。
    plan = gateway.plan(
        username="alice", data_source_id=source.id,
        sql="SELECT id FROM orders, customers",
    )
    assert "SHA2" in plan.sql.upper()


def test_masked_column_cannot_be_used_to_infer_original_value(tmp_path):
    gateway, source, access = setup_gateway(tmp_path)
    group = access.create_group("受限财务")
    access.add_group_member(group.id, "bob")
    access.grant_group(
        group_id=group.id, data_source_id=source.id, schema_name="erp",
        table_name="orders", allowed_columns=["id", "amount"], operations=["query"],
        masked_columns={"amount": "partial"},
    )
    with pytest.raises(QueryDenied) as err:
        gateway.plan(username="bob", data_source_id=source.id,
                     sql="SELECT id FROM orders WHERE amount > 100")
    assert err.value.code == "masked_column_usage"


@pytest.mark.parametrize("row_filter", [
    "id IN (SELECT id FROM customers)",
    "load_file('/etc/passwd') IS NULL",
    "other.orders.id = 1",
])
def test_unsafe_row_policy_is_denied_at_query_time(tmp_path, row_filter):
    gateway, source, access = setup_gateway(tmp_path)
    access.revoke(access.list_for_user("alice", source.id)[0].id)
    access.grant(
        username="alice", data_source_id=source.id, schema_name="erp",
        table_name="orders", allowed_columns=["id"], operations=["query"],
        row_filter_sql=row_filter,
    )
    with pytest.raises(QueryDenied) as err:
        gateway.plan(username="alice", data_source_id=source.id,
                     sql="SELECT id FROM orders")
    assert err.value.code == "invalid_row_policy"
