"""
LLM 与 Skill 之间的契约模型(architecture.md §C1/C2)。

关键概念:
- ComputationPlan: 结构化指令,客户端 skill 自动解析与执行
- LLMResponse: LLM 必须同时输出 scenario + computation_plan + summary
- AgentState: LangGraph 工作流的状态对象
- SchemaDescription: 用户数据的 schema(给 LLM 用,绝不含具体数据)

场景(Scenario)定义(architecture.md §三):
  1 描述性分析  → pandaseal
  2 数值计算    → henumpy
  3 经典 ML     → helearn (通常仅推理)
  4 DL 推理     → hetorch (不做训练)
  5 加密入库    → zfhe(独立,不经过 LLM)
  6 复合场景    → 流水线(多工具串联)
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# 场景与工具枚举
# ---------------------------------------------------------------------------


class Scenario(IntEnum):
    """场景标签(v4 简化,场景 6 已合并到 1)。"""

    DESCRIPTIVE = 1   # 数据分析:统计 / 分组 / 排名 / 比率
    NUMERICAL = 2     # 矩阵 / 向量(预留)
    CLASSICAL_ML = 3  # ML 训练/推理(预留)
    DL_INFERENCE = 4  # 神经网络推理(预留)
    INGESTION = 5     # 加密入库(不出 plan)


# 图表类型(Excel 内原生生成)
ChartType = Literal["line", "bar", "pie", "scatter", "heatmap"]


# ---------------------------------------------------------------------------
# Schema 描述(给 LLM 用)
# ---------------------------------------------------------------------------


class FieldSpec(BaseModel):
    """数据字段说明 —— 只给 LLM 看字段元数据,绝不含具体值。"""

    name: str
    type: Literal["int", "float", "string", "date", "datetime", "bool", "category"]
    description: Optional[str] = None


class SchemaDescription(BaseModel):
    """
    用户数据的 schema 描述,发送给 LLM 用。
    严格只含元数据,不含任何具体样本数据。
    """

    fields: list[FieldSpec]
    row_count_hint: Optional[Literal["small", "medium", "large"]] = None  # 量级提示
    description: Optional[str] = None  # 业务上下文,如"销售记录"


# ---------------------------------------------------------------------------
# Excel 输出规格
# ---------------------------------------------------------------------------


class ChartSpec(BaseModel):
    type: ChartType
    x: str
    y: Union[str, list[str]]
    title: Optional[str] = None


class SheetSpec(BaseModel):
    """一个 sheet 的输出说明。"""

    name: str
    columns: list[str] = Field(default_factory=list)
    chart: Optional[ChartSpec] = None


class ExcelOutput(BaseModel):
    """
    Excel 文件输出规格。
    file 必须以 ~/Downloads/ 开头(B6 第 2 条规则在 permissions 模块强制校验)。
    """

    file: str  # 形如 ~/Downloads/analysis_<timestamp>.xlsx
    # v3:新 SkillCall 路径下 sheets 不需要(每个 SkillCall 自带 sheet_name)。
    # 老 ops 路径下仍可传 SheetSpec 列表。
    sheets: list[SheetSpec] = Field(default_factory=list)

    @field_validator("file")
    @classmethod
    def file_must_be_in_downloads(cls, v: str) -> str:
        normalized = v.strip()
        if not (normalized.startswith("~/Downloads/") or normalized.startswith("/Users/")):
            raise ValueError("Excel 文件路径必须在 ~/Downloads/ 之内")
        if not normalized.endswith(".xlsx"):
            raise ValueError("Excel 文件必须以 .xlsx 结尾")
        return normalized


# ---------------------------------------------------------------------------
# SkillCall + ComputationPlan(v4 · 唯一主路径)
# ---------------------------------------------------------------------------


class SkillCall(BaseModel):
    """
    LLM 通过 skill_calls 表达计算意图。
    每个 SkillCall 对应 client/tools/skills.py 里的一个 skill 函数。
    产出一个 sheet,skill 内部自动合并 metadata 身份列。
    """

    skill: str                            # 必须在 SKILLS 注册表里
    params: dict[str, Any] = Field(default_factory=dict)
    sheet_name: Optional[str] = None      # 不传时由 skill 自定
    chart: Optional[ChartSpec] = None     # 可选;skill 会给默认 chart


class ComputationPlan(BaseModel):
    """
    LLM 输出的结构化计算指令(v4):scenario + skill_calls + output。
    弃用 v3 的 ops/tool/pipeline_steps 字段。
    """

    scenario: Scenario
    skill_calls: list[SkillCall] = Field(default_factory=list)
    output: Optional[ExcelOutput] = None

    @model_validator(mode="after")
    def _check_scenario_consistency(self) -> "ComputationPlan":
        sc = self.scenario
        if sc in (Scenario.DESCRIPTIVE, Scenario.NUMERICAL, Scenario.CLASSICAL_ML, Scenario.DL_INFERENCE):
            if not self.skill_calls:
                raise ValueError(f"场景 {sc.value} 必须提供至少一个 skill_call")
            if not self.output:
                raise ValueError(f"场景 {sc.value} 必须指定 output(Excel 文件)")
        elif sc == Scenario.INGESTION:
            pass
        # 场景 6 (PIPELINE) 与 INGESTION 一样为占位,v4 不要求
        return self


# ---------------------------------------------------------------------------
# LLM 响应
# ---------------------------------------------------------------------------


class LLMResponse(BaseModel):
    """LLM 每次回复必须包含 computation_plan + summary 两部分。"""

    computation_plan: ComputationPlan
    summary: str = Field(description="给用户看的自然语言,严禁含具体数值/日期/名称/样本")


# ---------------------------------------------------------------------------
# LangGraph 工作流状态
# ---------------------------------------------------------------------------


# v4 不再有 AgentState — 用 client/webui/pipeline.py 的单一函数路径
