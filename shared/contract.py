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
from typing_extensions import TypedDict


# ---------------------------------------------------------------------------
# 场景与工具枚举
# ---------------------------------------------------------------------------


class Scenario(IntEnum):
    """六个场景标签,LLM 必须在 computation_plan.scenario 中显式标注。"""

    DESCRIPTIVE = 1  # 汇总 / 分组 / 透视 / 时序
    NUMERICAL = 2  # 矩阵 / 向量 / 线性代数
    CLASSICAL_ML = 3  # 回归 / 分类 / 聚类 / 降维(通常仅推理)
    DL_INFERENCE = 4  # 神经网络推理(不做训练)
    INGESTION = 5  # 加密入库(独立,不经过 LLM 出方案)
    PIPELINE = 6  # 多步串联


# 计算层工具(场景 1-4 使用)
CalcTool = Literal["pandaseal", "henumpy", "helearn", "hetorch"]

# 图表类型(Excel 内原生生成)
ChartType = Literal["line", "bar", "pie", "scatter", "heatmap"]

# 场景 → 默认主工具映射
SCENARIO_DEFAULT_TOOL: dict[Scenario, Optional[str]] = {
    Scenario.DESCRIPTIVE: "pandaseal",
    Scenario.NUMERICAL: "henumpy",
    Scenario.CLASSICAL_ML: "helearn",
    Scenario.DL_INFERENCE: "hetorch",
    Scenario.INGESTION: "zfhe",
    Scenario.PIPELINE: None,  # 复合场景不固定主工具
}


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
    sheets: list[SheetSpec]

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
# 操作与计算方案
# ---------------------------------------------------------------------------


class Operation(BaseModel):
    """单个计算操作。具体语义由 op 字符串决定,不同工具支持的 op 不同。"""

    op: str
    field: Optional[str] = None
    fields: Optional[list[str]] = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _normalize_field_fields(cls, data: Any) -> Any:
        """
        LLM 兼容:经常会把多列名当成 list 塞到 `field`,或反过来。
        这里统一规范化:
        - field 是 list/tuple → 自动迁到 fields
        - fields 只有一个元素 → 同步落到 field
        - 都给了:fields 优先,field 取 fields[0]
        """
        if not isinstance(data, dict):
            return data
        f = data.get("field")
        fs = data.get("fields")
        # field 是列表 → 迁
        if isinstance(f, (list, tuple)):
            if not fs:
                fs = list(f)
            data["fields"] = list(fs)
            data["field"] = fs[0] if fs else None
        # 有 fields 但没 field → 用第一项做 field(老 op 实现按 field 拿单列时有 fallback)
        if isinstance(fs, list) and fs and not data.get("field"):
            data["field"] = fs[0]
        return data


class PipelineStep(BaseModel):
    """复合场景中的一个流水线步骤。"""

    tool: CalcTool
    ops: list[Operation]
    output_name: Optional[str] = None  # 中间结果命名


class ComputationPlan(BaseModel):
    """
    LLM 输出的结构化计算指令。

    - 场景 1-4:tool + ops + output 必填
    - 场景 5:tool=None,只做加密入库(实际上 LLM 不会输出这种 plan)
    - 场景 6:pipeline_steps 必填,顶层 tool/ops 不使用
    """

    scenario: Scenario
    tool: Optional[CalcTool] = None
    ops: list[Operation] = Field(default_factory=list)
    output: Optional[ExcelOutput] = None
    pipeline_steps: list[PipelineStep] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_scenario_consistency(self) -> "ComputationPlan":
        sc = self.scenario
        if sc in (Scenario.DESCRIPTIVE, Scenario.NUMERICAL, Scenario.CLASSICAL_ML, Scenario.DL_INFERENCE):
            if not self.tool:
                raise ValueError(f"场景 {sc.value} 必须指定 tool")
            expected = SCENARIO_DEFAULT_TOOL[sc]
            if self.tool != expected:
                # 允许但警告(后续可改成严格)—— 此处仅记录到 ops 不阻塞
                pass
            if not self.ops:
                raise ValueError(f"场景 {sc.value} 必须至少有一个 op")
            if not self.output:
                raise ValueError(f"场景 {sc.value} 必须指定 output(Excel 文件)")

        elif sc == Scenario.PIPELINE:
            if not self.pipeline_steps:
                raise ValueError("场景 6(复合)必须提供 pipeline_steps")
            if not self.output:
                raise ValueError("场景 6 必须指定最终 output")

        elif sc == Scenario.INGESTION:
            # 场景 5 一般不会由 LLM 出 plan,但允许构造空 plan
            pass

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


class AgentState(TypedDict, total=False):
    """
    LangGraph 工作流的状态对象(TypedDict 形态,LangGraph 推荐写法)。
    所有字段都是可选,节点逐步填充。
    """

    # --- 输入 ---
    user_query: str
    schema: dict  # SchemaDescription 序列化形式
    ciphertext_paths: list[str]  # 本地密文数据文件路径

    # --- LLM 阶段 ---
    llm_raw: Optional[dict]  # 原始 LLM 响应,调试用
    computation_plan: Optional[dict]  # ComputationPlan 序列化
    summary_raw: Optional[str]
    summary_filtered: Optional[str]
    summary_filter_hit: bool  # 是否被 B6 第 3 条过滤命中

    # --- 执行阶段 ---
    encrypted_input_paths: list[str]  # zfhe 加密后(若需)
    encrypted_result: Any  # 计算工具产出的密文结果
    decrypted_result: Any  # zfhe 解密后的明文,即将写入 Excel
    excel_path: Optional[str]  # 最终 Excel 文件路径

    # --- 元数据(明文标识列)---
    metadata_path: Optional[str]  # <cipher>.meta.csv 路径
    metadata_rows: Optional[list]  # 已加载的标识列(list of dict)
    metadata_columns: Optional[list[str]]  # 列名

    # --- 控制流 ---
    error: Optional[str]
    retry_count: int  # LLM 重试次数
    needs_authorization: bool  # 是否需要用户授权解密(B6 第 1 条)
    authorized: bool  # 用户是否已授权
