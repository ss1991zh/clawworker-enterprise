"""
契约层测试 —— v4 简化版,SkillCall + ComputationPlan。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.contract import (
    ChartSpec,
    ComputationPlan,
    ExcelOutput,
    Scenario,
    SheetSpec,
    SkillCall,
)


# ---------------------------------------------------------------------------
# ExcelOutput.file 路径校验
# ---------------------------------------------------------------------------


class TestExcelOutputPath:
    def test_accepts_downloads_path(self):
        out = ExcelOutput(file="~/Downloads/test.xlsx")
        assert out.file.endswith(".xlsx")

    def test_accepts_users_absolute_path(self):
        out = ExcelOutput(file="/Users/foo/Downloads/test.xlsx")
        assert out.file.endswith(".xlsx")

    def test_rejects_path_outside_downloads(self):
        with pytest.raises(ValidationError):
            ExcelOutput(file="/tmp/test.xlsx")

    def test_rejects_non_xlsx_extension(self):
        with pytest.raises(ValidationError):
            ExcelOutput(file="~/Downloads/test.csv")


# ---------------------------------------------------------------------------
# ComputationPlan 场景一致性
# ---------------------------------------------------------------------------


class TestComputationPlanScenarios:
    def _valid_output(self):
        return ExcelOutput(file="~/Downloads/test.xlsx")

    def test_descriptive_requires_skill_calls(self):
        with pytest.raises(ValidationError):
            ComputationPlan(
                scenario=Scenario.DESCRIPTIVE,
                skill_calls=[],
                output=self._valid_output(),
            )

    def test_descriptive_requires_output(self):
        with pytest.raises(ValidationError):
            ComputationPlan(
                scenario=Scenario.DESCRIPTIVE,
                skill_calls=[SkillCall(skill="describe", params={})],
            )

    def test_descriptive_valid(self):
        plan = ComputationPlan(
            scenario=Scenario.DESCRIPTIVE,
            skill_calls=[
                SkillCall(skill="group_stats", params={"by": "region"}, sheet_name="大区汇总")
            ],
            output=self._valid_output(),
        )
        assert plan.scenario == Scenario.DESCRIPTIVE
        assert len(plan.skill_calls) == 1
        assert plan.skill_calls[0].sheet_name == "大区汇总"

    def test_ingestion_no_skill_calls_ok(self):
        # INGESTION 不强制要求 skill_calls / output
        plan = ComputationPlan(scenario=Scenario.INGESTION)
        assert plan.scenario == Scenario.INGESTION


# ---------------------------------------------------------------------------
# ChartSpec
# ---------------------------------------------------------------------------


def test_chart_spec_multi_y():
    cs = ChartSpec(type="bar", x="month", y=["amount_sum", "count"])
    assert isinstance(cs.y, list)


def test_chart_spec_single_y():
    cs = ChartSpec(type="line", x="date", y="value")
    assert cs.y == "value"


# ---------------------------------------------------------------------------
# SkillCall
# ---------------------------------------------------------------------------


def test_skill_call_with_chart():
    sc = SkillCall(
        skill="top_n_by",
        params={"by": "region", "metric": "amount", "n": 10},
        sheet_name="TOP10",
        chart=ChartSpec(type="bar", x="region", y="amount"),
    )
    assert sc.chart.type == "bar"
    assert sc.params["n"] == 10
