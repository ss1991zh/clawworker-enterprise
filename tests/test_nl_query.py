from __future__ import annotations

import pytest

from host.nl_query import (
    NaturalQueryError,
    build_schema_prompt,
    generate_candidate,
)
from client.webui.pipeline import looks_like_analysis


CATALOG = [{
    "schema": "erp",
    "table": "orders",
    "columns": [
        {"name": "id", "type": "bigint", "mask": ""},
        {"name": "amount", "type": "decimal", "mask": "partial"},
    ],
}]


class Provider:
    def __init__(self, response):
        self.response = response
        self.system = ""
        self.user = ""

    def raw_chat(self, *, system, user):
        self.system, self.user = system, user
        return self.response


def test_prompt_contains_only_authorized_structure_and_intent():
    system, user = build_schema_prompt(
        engine="mysql", source_name="ERP", intent="统计订单金额", catalog=CATALOG,
    )
    assert "erp.orders" in user
    assert "amount decimal" in user
    assert "统计订单金额" in user
    assert "只返回 JSON" in system
    for secret in ("password", "db.internal", "region = '华东'", "SELECTED ROW"):
        assert secret not in system + user


@pytest.mark.parametrize("response", [
    "not json",
    "[]",
    '{"sql":"","explanation":"授权结构无法满足"}',
])
def test_invalid_or_empty_model_plan_is_rejected(response):
    with pytest.raises(NaturalQueryError):
        generate_candidate(
            Provider(response), engine="mysql", source_name="ERP",
            intent="查询订单", catalog=CATALOG,
        )


def test_candidate_can_be_read_from_json_fence_but_is_not_executed():
    provider = Provider(
        '```json\n{"sql":"SELECT id, amount FROM erp.orders",'
        '"explanation":"按授权字段查询"}\n```'
    )
    candidate = generate_candidate(
        provider, engine="mysql", source_name="ERP",
        intent="查询订单", catalog=CATALOG,
    )
    assert candidate.sql == "SELECT id, amount FROM erp.orders"
    assert candidate.explanation == "按授权字段查询"
    assert "不执行查询" in provider.system


def test_intent_length_is_bounded():
    with pytest.raises(NaturalQueryError):
        build_schema_prompt(
            engine="mysql", source_name="ERP", intent="查" * 2001, catalog=CATALOG,
        )


def test_prompt_does_not_include_catalog_fields_outside_explicit_contract():
    catalog = [{
        **CATALOG[0],
        "database_host": "db.internal",
        "username": "reader",
        "password": "do-not-leak",
        "row_filter_sql": "region = '华东'",
        "sample_rows": [{"id": 1, "amount": 999}],
    }]
    _, user = build_schema_prompt(
        engine="mysql", source_name="ERP", intent="查订单", catalog=catalog,
    )
    for secret in ("db.internal", "reader", "do-not-leak", "region", "999"):
        assert secret not in user


def test_natural_query_prompt_explicitly_forbids_execution_and_data_claims():
    system, _ = build_schema_prompt(
        engine="mysql", source_name="ERP", intent="查订单", catalog=CATALOG,
    )
    assert "不执行查询" in system
    assert "不声称已经查到结果" in system


def test_database_query_result_is_routed_to_encrypted_data_analysis_not_web_chat():
    assert looks_like_analysis("分析数据库查询结果并汇总金额")
