"""用户分析需求的结构化指标模型。

这里只处理字段名和公式依赖，不读取任何数据行，因此管理端和用户端都可安全复用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Literal


RequirementStatus = Literal["unknown", "direct", "derived", "missing"]


@dataclass(frozen=True)
class DependencyOption:
    """满足一个指标所需的一组字段语义；多个 option 之间是“或”。"""

    concepts: tuple[str, ...]
    formula: str = ""


@dataclass(frozen=True)
class MetricRequirement:
    label: str
    subject: str = ""
    comparison: str = ""
    options: tuple[DependencyOption, ...] = ()


@dataclass(frozen=True)
class MetricAssessment:
    requirement: MetricRequirement
    status: RequirementStatus
    selected_columns: tuple[str, ...] = ()
    missing_concepts: tuple[str, ...] = ()
    formula: str = ""


@dataclass
class AnalysisRequirements:
    query: str
    metrics: list[MetricRequirement] = field(default_factory=list)

    @property
    def labels(self) -> list[str]:
        return [metric.label for metric in self.metrics]


_OUTPUT_METRICS = (
    "库存周转率", "库存周转天数", "周转天数",
    "目标完成率", "完成率", "达成率",
    "边际贡献率", "边际贡献", "毛利率", "毛利",
    "回款率", "差异率", "同比增长率", "环比增长率",
)


_CONCEPT_COLUMNS: dict[str, tuple[str, ...]] = {
    "time": (
        "month_start", "month", "date", "order_date", "invoice_date", "月份", "日期", "年度",
    ),
    "revenue": (
        "net_revenue", "revenue", "sales_revenue", "sales_amount", "营业收入", "销售收入",
        "收入", "营收", "销售额",
    ),
    "gross_profit": ("gross_profit", "毛利", "毛利润"),
    "cost": (
        "standard_cost", "cost_amount", "sales_cost", "cost_of_goods_sold", "cogs",
        "营业成本", "销售成本", "成本",
    ),
    "inventory": ("average_inventory", "avg_inventory", "平均库存", "平均存货"),
    "opening_inventory": ("opening_inventory", "begin_inventory", "期初库存", "期初存货"),
    "closing_inventory": ("closing_inventory", "ending_inventory", "期末库存", "期末存货"),
    "target": ("target", "target_amount", "budget", "目标", "预算"),
    "actual": ("actual", "actual_amount", "完成额", "实际值", "实际"),
    "receivable": ("receivable", "accounts_receivable", "应收", "应收账款"),
    "collection": ("collection", "collected_amount", "回款", "回款金额"),
    "variable_cost": ("variable_cost", "变动成本"),
}


def _options_for(label: str) -> tuple[DependencyOption, ...]:
    if label == "收入":
        return (DependencyOption(("revenue",)),)
    if label == "毛利":
        return (
            DependencyOption(("gross_profit",)),
            DependencyOption(("revenue", "cost"), "收入 - 成本"),
        )
    if label == "毛利率":
        return (
            DependencyOption(("gross_profit", "revenue"), "毛利 / 收入"),
            DependencyOption(("revenue", "cost"), "(收入 - 成本) / 收入"),
        )
    if "库存周转率" in label:
        return (
            DependencyOption(("cost", "inventory"), "销售成本 / 平均库存"),
            DependencyOption(
                ("cost", "opening_inventory", "closing_inventory"),
                "销售成本 / ((期初库存 + 期末库存) / 2)",
            ),
        )
    if "周转天数" in label:
        return _options_for("库存周转率")
    if label in {"目标完成率", "完成率", "达成率"}:
        return (DependencyOption(("actual", "target"), "实际值 / 目标值"),)
    if label == "回款率":
        return (DependencyOption(("collection", "receivable"), "回款金额 / 应收金额"),)
    if label == "边际贡献":
        return (DependencyOption(("revenue", "variable_cost"), "收入 - 变动成本"),)
    if label == "边际贡献率":
        return (
            DependencyOption(("revenue", "variable_cost"), "(收入 - 变动成本) / 收入"),
        )
    if label.endswith("同比增长率"):
        subject = label.removesuffix("同比增长率")
        base = _options_for(subject) or (DependencyOption((subject,)),)
        return tuple(
            DependencyOption(("time",) + option.concepts, f"{option.formula or subject} 的上年同期增长率")
            for option in base
        )
    if label.endswith("环比增长率"):
        subject = label.removesuffix("环比增长率")
        base = _options_for(subject) or (DependencyOption((subject,)),)
        return tuple(
            DependencyOption(("time",) + option.concepts, f"{option.formula or subject} 的上期增长率")
            for option in base
        )
    return ()


def extract_analysis_requirements(user_query: str) -> AnalysisRequirements:
    """把用户明确点名的指标和比较关系转换为稳定结构。"""
    query = user_query or ""
    found = [metric for metric in _OUTPUT_METRICS if metric in query]
    found.sort(key=len, reverse=True)
    labels = [
        metric for index, metric in enumerate(found)
        if not any(metric in longer for longer in found[:index])
    ]

    subjects: list[str] = []
    if any(alias in query for alias in ("营业收入", "公司收入", "收入", "营收", "销售额")):
        subjects.append("收入")
    if "毛利" in query and "毛利率" not in query:
        subjects.append("毛利")
    for subject in subjects:
        if subject not in labels:
            labels.append(subject)

    for marker, suffix in (("同比", "同比增长率"), ("环比", "环比增长率")):
        if marker not in query:
            continue
        if subjects:
            labels = [label for label in labels if label != suffix]
            for subject in subjects:
                expanded = f"{subject}{suffix}"
                if expanded not in labels:
                    labels.append(expanded)
        elif suffix not in labels:
            labels.append(suffix)

    metrics = []
    for label in labels:
        comparison = "同比" if "同比" in label else "环比" if "环比" in label else ""
        subject = label.replace("同比增长率", "").replace("环比增长率", "") if comparison else label
        metrics.append(MetricRequirement(
            label=label,
            subject=subject,
            comparison=comparison,
            options=_options_for(label),
        ))
    return AnalysisRequirements(query=query, metrics=metrics)


def _normalize(value: str) -> str:
    return re.sub(r"[\s（）()_\-]+", "", str(value or "")).lower()


def _concept_matches(concept: str, columns: Iterable[str]) -> list[str]:
    candidates = _CONCEPT_COLUMNS.get(concept, (concept,))
    normalized_candidates = {_normalize(candidate) for candidate in candidates}
    return [
        column for column in columns
        if _normalize(column) in normalized_candidates
        or any(candidate in _normalize(column) for candidate in normalized_candidates if len(candidate) >= 3)
    ]


def assess_catalog(
    requirements: AnalysisRequirements,
    catalog: list[dict],
) -> list[MetricAssessment]:
    """只依据授权结构判断直接字段、可靠推导或明确缺参。"""
    columns = [
        str(column.get("name") or "")
        for table in catalog
        for column in table.get("columns", [])
        if column.get("name")
    ]
    assessments: list[MetricAssessment] = []
    for requirement in requirements.metrics:
        if not requirement.options:
            assessments.append(MetricAssessment(requirement, "unknown"))
            continue
        best_missing: tuple[str, ...] = ()
        for option_index, option in enumerate(requirement.options):
            selected: list[str] = []
            missing: list[str] = []
            for concept in option.concepts:
                matches = _concept_matches(concept, columns)
                if matches:
                    selected.append(matches[0])
                else:
                    missing.append(concept)
            if not missing:
                status: RequirementStatus = "direct" if option_index == 0 and not option.formula else "derived"
                assessments.append(MetricAssessment(
                    requirement, status, tuple(dict.fromkeys(selected)), (), option.formula,
                ))
                break
            if not best_missing or len(missing) < len(best_missing):
                best_missing = tuple(missing)
        else:
            assessments.append(MetricAssessment(
                requirement, "missing", (), best_missing,
            ))
    return assessments


def catalog_assessment_text(assessments: list[MetricAssessment]) -> str:
    """生成给查询规划器的结构化提示，不含数据值。"""
    lines = []
    for item in assessments:
        if item.status in {"direct", "derived"}:
            mode = "直接字段" if item.status == "direct" else "可可靠推导"
            formula = f"；公式：{item.formula}" if item.formula else ""
            lines.append(
                f"- {item.requirement.label}：{mode}；候选字段：{', '.join(item.selected_columns)}{formula}"
            )
        elif item.status == "missing":
            lines.append(
                f"- {item.requirement.label}：缺少必要参数：{', '.join(item.missing_concepts)}；应跳过该指标"
            )
        else:
            lines.append(f"- {item.requirement.label}：需由规划器根据字段语义进一步判断")
    return "\n".join(lines)
