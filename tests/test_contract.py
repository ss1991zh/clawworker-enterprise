"""
契约层测试 —— pydantic 模型校验。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from shared.contract import (
    ChartSpec,
    ComputationPlan,
    ExcelOutput,
    Operation,
    Scenario,
    SheetSpec,
)


# ---------------------------------------------------------------------------
# ExcelOutput.file 路径校验
# ---------------------------------------------------------------------------


class TestExcelOutputPath:
    def test_accepts_downloads_path(self):
        out = ExcelOutput(
            file="~/Downloads/test.xlsx",
            sheets=[SheetSpec(name="S1")],
        )
        assert out.file.endswith(".xlsx")

    def test_rejects_path_outside_downloads(self):
        with pytest.raises(ValidationError):
            ExcelOutput(file="/tmp/test.xlsx", sheets=[SheetSpec(name="S1")])

    def test_rejects_non_xlsx_extension(self):
        with pytest.raises(ValidationError):
            ExcelOutput(file="~/Downloads/test.csv", sheets=[SheetSpec(name="S1")])


# ---------------------------------------------------------------------------
# ComputationPlan 场景一致性
# ---------------------------------------------------------------------------


class TestComputationPlanScenarios:
    def _valid_output(self):
        return ExcelOutput(
            file="~/Downloads/test.xlsx",
            sheets=[SheetSpec(name="S1")],
        )

    def test_scenario_1_requires_tool(self):
        with pytest.raises(ValidationError):
            ComputationPlan(
                scenario=Scenario.DESCRIPTIVE,
                ops=[Operation(op="sum")],
                output=self._valid_output(),
            )

    def test_scenario_1_requires_ops(self):
        with pytest.raises(ValidationError):
            ComputationPlan(
                scenario=Scenario.DESCRIPTIVE,
                tool="pandaseal",
                output=self._valid_output(),
            )

    def test_scenario_1_requires_output(self):
        with pytest.raises(ValidationError):
            ComputationPlan(
                scenario=Scenario.DESCRIPTIVE,
                tool="pandaseal",
                ops=[Operation(op="sum")],
            )

    def test_scenario_1_valid(self):
        plan = ComputationPlan(
            scenario=Scenario.DESCRIPTIVE,
            tool="pandaseal",
            ops=[Operation(op="group_by", field="month")],
            output=self._valid_output(),
        )
        assert plan.scenario == Scenario.DESCRIPTIVE

    def test_scenario_6_requires_pipeline_steps(self):
        with pytest.raises(ValidationError):
            ComputationPlan(
                scenario=Scenario.PIPELINE,
                output=self._valid_output(),
            )


# ---------------------------------------------------------------------------
# ChartSpec
# ---------------------------------------------------------------------------


def test_chart_spec_multi_y():
    cs = ChartSpec(type="bar", x="month", y=["amount_sum", "count"])
    assert isinstance(cs.y, list)
