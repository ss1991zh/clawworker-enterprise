from shared.analysis_requirements import (
    assess_catalog,
    extract_analysis_requirements,
)


def _catalog(*columns):
    return [{
        "schema": "demo",
        "table": "monthly_sales",
        "columns": [{"name": name, "type": "decimal"} for name in columns],
    }]


def test_compound_yoy_request_expands_each_subject():
    requirements = extract_analysis_requirements("计算公司收入、毛利和同比增长")
    assert requirements.labels == [
        "毛利", "收入", "收入同比增长率", "毛利同比增长率",
    ]


def test_catalog_assessment_prefers_direct_then_reliable_derivation():
    requirements = extract_analysis_requirements("计算公司收入、毛利和同比增长")
    assessments = assess_catalog(
        requirements,
        _catalog("month_start", "net_revenue", "standard_cost"),
    )
    by_label = {item.requirement.label: item for item in assessments}
    assert by_label["收入"].status == "direct"
    assert by_label["毛利"].status == "derived"
    assert by_label["毛利同比增长率"].status == "derived"
    assert set(by_label["毛利同比增长率"].selected_columns) == {
        "month_start", "net_revenue", "standard_cost",
    }


def test_catalog_assessment_marks_only_unavailable_metric_missing():
    requirements = extract_analysis_requirements("计算公司收入、毛利和同比增长")
    assessments = assess_catalog(
        requirements,
        _catalog("month_start", "net_revenue"),
    )
    by_label = {item.requirement.label: item for item in assessments}
    assert by_label["收入同比增长率"].status in {"direct", "derived"}
    assert by_label["毛利"].status == "missing"
    assert by_label["毛利同比增长率"].status == "missing"


def test_inventory_turnover_supports_average_or_opening_closing_inventory():
    requirements = extract_analysis_requirements("计算所有库存周转率")
    direct = assess_catalog(
        requirements,
        _catalog("cost_of_goods_sold", "average_inventory"),
    )[0]
    derived = assess_catalog(
        requirements,
        _catalog("cost_of_goods_sold", "opening_inventory", "closing_inventory"),
    )[0]
    assert direct.status == "derived"
    assert derived.status == "derived"
    assert "期初库存" in derived.formula
