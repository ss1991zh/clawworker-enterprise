"""
pytest fixtures —— 复用的测试构件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from client.permissions import AutoApproveAuthorizer
from client.tools import HELearn, HENumpy, HETorch, PandaSeal, ZFHE
from shared.contract import (
    ChartSpec,
    ComputationPlan,
    ExcelOutput,
    LLMResponse,
    Operation,
    Scenario,
    SheetSpec,
)


# ---------------------------------------------------------------------------
# 工具实例
# ---------------------------------------------------------------------------


@pytest.fixture
def zfhe() -> ZFHE:
    return ZFHE()


@pytest.fixture
def pandaseal() -> PandaSeal:
    return PandaSeal()


@pytest.fixture
def henumpy() -> HENumpy:
    return HENumpy()


@pytest.fixture
def helearn() -> HELearn:
    return HELearn()


@pytest.fixture
def hetorch() -> HETorch:
    return HETorch()


@pytest.fixture
def authorizer():
    return AutoApproveAuthorizer()


# ---------------------------------------------------------------------------
# 样例 ComputationPlan
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_excel_output(tmp_downloads: Path) -> ExcelOutput:
    return ExcelOutput(
        file="~/Downloads/analysis_test.xlsx",
        sheets=[
            SheetSpec(
                name="MonthlyTrend",
                columns=["group", "amount_sum"],
                chart=ChartSpec(type="line", x="group", y="amount_sum", title="月度趋势"),
            )
        ],
    )


@pytest.fixture
def sample_plan_scenario_1(sample_excel_output: ExcelOutput) -> ComputationPlan:
    return ComputationPlan(
        scenario=Scenario.DESCRIPTIVE,
        tool="pandaseal",
        ops=[
            Operation(op="group_by", field="month"),
            Operation(op="sum", field="amount"),
        ],
        output=sample_excel_output,
    )


@pytest.fixture
def sample_llm_response_scenario_1(sample_plan_scenario_1: ComputationPlan) -> LLMResponse:
    return LLMResponse(
        computation_plan=sample_plan_scenario_1,
        summary="已按月份聚合金额并生成折线图,结果见 Excel 的 MonthlyTrend sheet。",
    )


# ---------------------------------------------------------------------------
# 临时 Downloads 目录(测试隔离)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_downloads(tmp_path: Path, monkeypatch) -> Path:
    """把 ~/Downloads/ 重定向到临时目录,避免污染真实用户目录。"""
    fake = tmp_path / "Downloads"
    fake.mkdir(parents=True, exist_ok=True)
    # 修改 permissions 与 excel_output 中的 DOWNLOADS_DIR
    monkeypatch.setattr("client.permissions.DOWNLOADS_DIR", fake)
    monkeypatch.setattr("client.excel_output.DOWNLOADS_DIR", fake)
    return fake


# ---------------------------------------------------------------------------
# 样例数据(明文,只用于测试)
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_sales_rows() -> list[dict]:
    return [
        {"month": "2024-01", "amount": 100, "category": "A"},
        {"month": "2024-01", "amount": 200, "category": "B"},
        {"month": "2024-02", "amount": 150, "category": "A"},
        {"month": "2024-02", "amount": 250, "category": "B"},
        {"month": "2024-03", "amount": 300, "category": "A"},
    ]


@pytest.fixture
def sample_ciphertext_file(tmp_path: Path, zfhe: ZFHE, sample_sales_rows: list[dict]) -> Path:
    """用 STUB zfhe 加密一份销售数据,写到临时密文文件。"""
    cipher = zfhe.encrypt(sample_sales_rows)
    p = tmp_path / "sales.cipher"
    p.write_bytes(cipher)
    return p


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------


class FixedLLMClient:
    """根据预设序列返回 LLMResponse,记录每次调用。"""

    def __init__(self, responses: list[LLMResponse]):
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def chat(self, system: str, user: str) -> LLMResponse:
        self.calls.append((system, user))
        if not self._responses:
            raise RuntimeError("FixedLLMClient 已没有可返回的响应")
        return self._responses.pop(0)


@pytest.fixture
def fixed_llm_factory():
    def _make(responses):
        return FixedLLMClient(responses)

    return _make
