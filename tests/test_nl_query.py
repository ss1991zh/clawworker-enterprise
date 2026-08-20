from __future__ import annotations

import json

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


class SequenceProvider(Provider):
    def __init__(self, responses):
        super().__init__(responses[0])
        self.responses = list(responses)
        self.calls = 0

    def raw_chat(self, *, system, user):
        self.system, self.user = system, user
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        return response


class FailIfCalledProvider:
    def raw_chat(self, *, system, user):
        raise AssertionError("确定性财务同比查询不应调用模型生成 SQL")


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


def test_mysql_yoy_prompt_requires_direct_date_arithmetic():
    system, _ = build_schema_prompt(
        engine="mysql", source_name="ERP", intent="计算收入同比", catalog=CATALOG,
    )
    assert "DATE_SUB" in system
    assert "STR_TO_DATE" in system
    assert "去年同期值" in system


def test_financial_yoy_uses_deterministic_authorized_base_query():
    catalog = [{
        "schema": "clawworker_sales_demo",
        "table": "v_management_pnl_monthly",
        "columns": [
            {"name": "month_start", "type": "varchar(10)", "mask": ""},
            {"name": "net_revenue", "type": "decimal(39,2)", "mask": ""},
            {"name": "gross_profit", "type": "decimal(38,2)", "mask": ""},
        ],
    }]

    candidate = generate_candidate(
        FailIfCalledProvider(), engine="mysql", source_name="管理会计测试库",
        intent="计算公司收入、毛利和同比增长", catalog=catalog,
    )

    assert "v_management_pnl_monthly" in candidate.sql
    assert "month_start" in candidate.sql
    assert "net_revenue" in candidate.sql
    assert "gross_profit" in candidate.sql
    assert "JOIN" not in candidate.sql.upper()
    assert "DATE_" not in candidate.sql.upper()
    assert "本机加密后计算同比" in candidate.explanation


def test_financial_yoy_can_select_indirect_profit_inputs_or_partial_inputs():
    indirect_catalog = [{
        "schema": "erp", "table": "monthly_pnl",
        "columns": [
            {"name": "month_start", "type": "varchar(10)", "mask": ""},
            {"name": "net_revenue", "type": "decimal", "mask": ""},
            {"name": "standard_cost", "type": "decimal", "mask": ""},
        ],
    }]
    indirect = generate_candidate(
        FailIfCalledProvider(), engine="mysql", source_name="ERP",
        intent="计算收入、毛利和同比", catalog=indirect_catalog,
    )
    assert "standard_cost" in indirect.sql
    assert "gross_profit" not in indirect.sql

    partial_catalog = [{
        "schema": "erp", "table": "monthly_pnl",
        "columns": [
            {"name": "month_start", "type": "varchar(10)", "mask": ""},
            {"name": "net_revenue", "type": "decimal", "mask": ""},
        ],
    }]
    partial = generate_candidate(
        FailIfCalledProvider(), engine="mysql", source_name="ERP",
        intent="计算收入、毛利和同比", catalog=partial_catalog,
    )
    assert "net_revenue" in partial.sql
    assert "gross_profit" not in partial.sql


def test_fragile_mysql_yoy_date_join_is_regenerated():
    catalog = [{
        "schema": "erp", "table": "monthly_sales",
        "columns": [
            {"name": "month_start", "type": "date", "mask": ""},
            {"name": "revenue", "type": "decimal", "mask": ""},
        ],
    }]
    bad = json.dumps({
        "sql": "SELECT a.month_start, a.revenue FROM erp.monthly_sales a "
               "LEFT JOIN erp.monthly_sales b ON b.month_start = "
               "DATE_FORMAT(DATE_SUB(STR_TO_DATE(CONCAT(a.month_start, '-01'), "
               "'%Y-%m-%d'), INTERVAL 1 YEAR), '%Y-%m-%d')",
        "explanation": "同比",
    })
    good = json.dumps({
        "sql": "SELECT a.month_start, a.revenue, b.revenue AS prior_revenue "
               "FROM erp.monthly_sales a LEFT JOIN erp.monthly_sales b "
               "ON b.month_start = DATE_SUB(a.month_start, INTERVAL 1 YEAR)",
        "explanation": "直接日期连接",
    })
    provider = SequenceProvider([bad, good])

    candidate = generate_candidate(
        provider, engine="mysql", source_name="ERP",
        intent="计算收入同比", catalog=catalog,
    )

    assert provider.calls == 2
    assert "STR_TO_DATE" not in candidate.sql
    assert "DATE_SUB(a.month_start" in candidate.sql


def test_repeated_fragile_mysql_yoy_join_is_repaired_deterministically():
    catalog = [{
        "schema": "erp", "table": "monthly_sales",
        "columns": [
            {"name": "month_start", "type": "date", "mask": ""},
            {"name": "revenue", "type": "decimal", "mask": ""},
        ],
    }]
    bad = json.dumps({
        "sql": "SELECT a.month_start, a.revenue, b.revenue AS prior_revenue "
               "FROM erp.monthly_sales a LEFT JOIN erp.monthly_sales b "
               "ON b.month_start = DATE_FORMAT(DATE_SUB(STR_TO_DATE("
               "CONCAT(a.month_start, '-01'), '%Y-%m-%d'), INTERVAL 1 YEAR), "
               "'%Y-%m-%d')",
        "explanation": "同比",
    })
    provider = SequenceProvider([bad, bad])

    candidate = generate_candidate(
        provider, engine="mysql", source_name="ERP",
        intent="计算收入同比", catalog=catalog,
    )

    assert provider.calls == 2
    assert "STR_TO_DATE" not in candidate.sql
    assert "DATE_FORMAT" not in candidate.sql
    assert "DATE_SUB(a.month_start, INTERVAL '1' YEAR)" in candidate.sql
    assert "已自动修正" in candidate.explanation


def test_varchar_10_iso_date_yoy_join_removes_only_extra_day_concat():
    catalog = [{
        "schema": "erp", "table": "monthly_sales",
        "columns": [
            {"name": "month_start", "type": "varchar(10)", "mask": ""},
            {"name": "revenue", "type": "decimal", "mask": ""},
        ],
    }]
    bad = json.dumps({
        "sql": "SELECT a.month_start, a.revenue, b.revenue AS prior_revenue "
               "FROM erp.monthly_sales a LEFT JOIN erp.monthly_sales b "
               "ON b.month_start = DATE_FORMAT(DATE_SUB(STR_TO_DATE("
               "CONCAT(a.month_start, '-01'), '%Y-%m-%d'), INTERVAL 1 YEAR), "
               "'%Y-%m-%d')",
        "explanation": "同比",
    })
    provider = SequenceProvider([bad, bad])

    candidate = generate_candidate(
        provider, engine="mysql", source_name="ERP",
        intent="计算收入同比", catalog=catalog,
    )

    assert "CONCAT" not in candidate.sql
    assert "STR_TO_DATE(a.month_start, '%Y-%m-%d')" in candidate.sql
    assert "DATE_SUB" in candidate.sql


def test_database_query_result_is_routed_to_encrypted_data_analysis_not_web_chat():
    assert looks_like_analysis("分析数据库查询结果并汇总金额")
