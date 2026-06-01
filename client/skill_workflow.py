"""
B3 Skill 工作流(architecture.md §B3)—— 整个系统的执行核心。

用 LangGraph 实现状态机:
  prepare → call_llm → validate_plan → filter_summary → route_by_scenario
                                                       ↓
       authorize → encrypt_input → compute_<scenario> → decrypt → write_excel → END

关键设计:
- 节点函数都接收 AgentState,返回 partial state(LangGraph 自动 merge)
- 工具与授权器通过 build_workflow 注入,便于测试
- summary 过滤命中 → 限次重试,超限走 fallback
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from client.excel_output import KpiCard, SheetData, WriteResult, make_excel_path, write_excel
from client.llm_client import LLMClient
from client.permissions import (
    FALLBACK_SUMMARY,
    DecryptionAuthorizer,
    enforce_excel_path,
    scan_summary,
)
from client.tools import HELearn, HENumpy, HETorch, PandaSeal, ZFHE
from shared.contract import (
    AgentState,
    ChartSpec,
    ComputationPlan,
    ExcelOutput,
    LLMResponse,
    Operation,
    Scenario,
    SheetSpec,
)
from shared.prompts import build_user_message, load_system_prompt


# ===========================================================================
# 依赖容器(注入到节点闭包)
# ===========================================================================


@dataclass
class WorkflowDeps:
    llm: LLMClient
    zfhe: ZFHE
    pandaseal: PandaSeal
    henumpy: HENumpy
    helearn: HELearn
    hetorch: HETorch
    authorizer: DecryptionAuthorizer
    max_retries: int = 2
    system_prompt: Optional[str] = None  # 缓存 system prompt


# ===========================================================================
# 节点函数
# ===========================================================================


def _prepare_request(state: AgentState) -> dict:
    """
    初始化重试计数 + 自动发现密文文件旁的 .meta.csv 标识列 sidecar。

    sidecar 约定:若 ciphertext_paths[0] = X.csv,会自动找 X.csv.meta.csv;
    存在则读为 list of dict,后续 renderer 用它合并标识列到输出 Excel。
    """
    out: dict = {
        "retry_count": state.get("retry_count", 0),
        "summary_filter_hit": False,
        "needs_authorization": True,
        "authorized": False,
    }
    # 自动发现 metadata sidecar
    cipher_paths = state.get("ciphertext_paths") or []
    if cipher_paths and not state.get("metadata_path"):
        cipher = Path(cipher_paths[0])
        candidate = cipher.with_suffix(cipher.suffix + ".meta.csv")
        if candidate.exists():
            out["metadata_path"] = str(candidate)

    # 加载 metadata(若有)
    meta_path = out.get("metadata_path") or state.get("metadata_path")
    if meta_path:
        try:
            import pandas as pd

            df = pd.read_csv(meta_path)
            out["metadata_rows"] = df.to_dict("records")
            out["metadata_columns"] = list(df.columns)
        except Exception as e:
            out["error"] = f"读取 metadata 失败: {e}"
    return out


def _make_call_llm(deps: WorkflowDeps):
    def call_llm(state: AgentState) -> dict:
        system = deps.system_prompt or load_system_prompt()
        user_msg = build_user_message(
            user_query=state["user_query"],
            schema_json=json.dumps(state["schema"], ensure_ascii=False),
        )
        try:
            llm_resp = deps.llm.chat(system=system, user=user_msg)
        except Exception as e:
            return {"error": f"LLM 调用失败: {e}"}

        return {
            "llm_raw": llm_resp.model_dump(),
            "computation_plan": llm_resp.computation_plan.model_dump(),
            "summary_raw": llm_resp.summary,
        }

    return call_llm


def _validate_plan(state: AgentState) -> dict:
    """对 computation_plan 做 pydantic 校验。"""
    # 上游已有 error 不要覆盖
    if state.get("error"):
        return {}
    plan_dict = state.get("computation_plan")
    if not plan_dict:
        return {"error": "缺少 computation_plan"}
    try:
        ComputationPlan.model_validate(plan_dict)
    except ValidationError as e:
        return {"error": f"plan 校验失败: {e}"}
    return {}


def _filter_summary(state: AgentState, extra_blocklist: Optional[list[str]] = None) -> dict:
    """B6 第 3 条:summary 内容过滤。"""
    raw = state.get("summary_raw", "")
    result = scan_summary(raw, extra_blocklist=extra_blocklist)
    if result.clean:
        return {"summary_filtered": raw, "summary_filter_hit": False}
    # 命中:不直接展示,等待重试或 fallback
    return {
        "summary_filter_hit": True,
        "summary_filtered": None,
        "error": f"summary 命中明文模式: {result.report()}",
    }


def _make_authorize(deps: WorkflowDeps):
    """B6 第 1 条:解密前授权。**必须在 compute 之后、decrypt 之前调用**。"""

    def authorize(state: AgentState) -> dict:
        # 上游(filter / compute)若已 error,直接透传,不打扰用户
        if state.get("error"):
            return {}
        plan = state.get("computation_plan", {})
        scenario = plan.get("scenario")
        reason = f"场景 {scenario} 计算完成,需解密密文结果以写入 Excel"
        ok = deps.authorizer.request(reason=reason)
        return {"authorized": ok, "needs_authorization": False}

    return authorize


def _make_compute(deps: WorkflowDeps, tool_name: str):
    """生成针对单一计算工具的节点函数。

    backend="stub":把文件读成 bytes 传给 tool
    backend="real":把文件路径(字符串)传给 tool,由工具内部用 ps.read_csv 等加载
    """
    tool_map = {
        "pandaseal": deps.pandaseal,
        "henumpy": deps.henumpy,
        "helearn": deps.helearn,
        "hetorch": deps.hetorch,
    }

    def compute(state: AgentState) -> dict:
        tool = tool_map[tool_name]
        plan = ComputationPlan.model_validate(state["computation_plan"])
        ops = plan.ops

        paths = state.get("ciphertext_paths", [])
        if not paths:
            return {"error": "没有可用的密文输入文件"}

        # 根据 backend 决定传递形式
        backend = getattr(tool, "backend", "stub")
        if backend == "real":
            cipher_in: Any = paths[0]  # 路径字符串
        else:
            cipher_in = Path(paths[0]).read_bytes()

        try:
            cipher_out = tool.run(ops, cipher_in)
        except Exception as e:
            return {"error": f"{tool_name} 执行失败: {e}"}

        return {"encrypted_result": cipher_out}

    return compute


def _make_compute_pipeline(deps: WorkflowDeps):
    """场景 6:按 pipeline_steps 顺序串联多工具。"""

    def compute_pipeline(state: AgentState) -> dict:
        plan = ComputationPlan.model_validate(state["computation_plan"])
        tool_map = {
            "pandaseal": deps.pandaseal,
            "henumpy": deps.henumpy,
            "helearn": deps.helearn,
            "hetorch": deps.hetorch,
        }
        paths = state.get("ciphertext_paths", [])
        if not paths:
            return {"error": "没有可用的密文输入文件"}
        cipher = Path(paths[0]).read_bytes()
        try:
            for step in plan.pipeline_steps:
                tool = tool_map[step.tool]
                cipher = tool.run(step.ops, cipher)
        except Exception as e:
            return {"error": f"pipeline 执行失败: {e}"}
        return {"encrypted_result": cipher}

    return compute_pipeline


def _make_decrypt(deps: WorkflowDeps):
    def decrypt(state: AgentState) -> dict:
        # 上游节点若已 error,直接透传(不再覆盖)
        if state.get("error"):
            return {}
        cipher = state.get("encrypted_result")
        if cipher is None:
            return {"error": "无密文结果可解密"}
        try:
            plain = deps.zfhe.decrypt(cipher)
        except Exception as e:
            return {"error": f"解密失败: {e}"}
        return {"decrypted_result": plain}

    return decrypt


def _write_excel_node(state: AgentState) -> dict:
    """根据 plan.output 渲染解密结果,写入 Excel。

    若 state 有 metadata_rows 且 decrypted 是 row-aligned list,
    自动合并明文标识列到输出。
    """
    if state.get("error"):
        return {}
    plan = ComputationPlan.model_validate(state["computation_plan"])
    output: ExcelOutput = plan.output
    if not output:
        return {"error": "plan.output 缺失,无法写 Excel"}

    if "decrypted_result" not in state:
        return {"error": "缺 decrypted_result"}
    decrypted = state["decrypted_result"]
    sheets = _render_to_sheets(
        decrypted,
        output.sheets,
        scenario=plan.scenario,
        metadata_rows=state.get("metadata_rows"),
        metadata_columns=state.get("metadata_columns"),
        ops=plan.ops,
    )

    # 文件路径:LLM 可能给了相对 ~/Downloads/ 的路径,统一规范化
    final_path = make_excel_path()
    enforce_excel_path(final_path)  # B6 第 2 条
    try:
        result: WriteResult = write_excel(sheets, path=final_path)
    except Exception as e:
        return {"error": f"Excel 写入失败: {e}"}
    return {"excel_path": str(result.path)}


# ---------------------------------------------------------------------------
# 渲染:解密结果 → SheetData
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# 产品级输出常量(中文化、格式、档位涂色)
# ---------------------------------------------------------------------------

# 常见英文 ratio 列名 → 中文对照
EN_TO_ZH_COLUMN_NAMES: dict[str, str] = {
    # 销售/财务
    "completion_rate": "目标完成率",
    "achievement_rate": "目标完成率",
    "target_completion_rate": "目标完成率",
    "collection_rate": "回款率",
    "payback_rate": "回款率",
    "growth_rate": "增长率",
    "margin_rate": "边际贡献率",
    "contribution_rate": "边际贡献率",
    "commission_rate": "提成比例",
    # 库存/成本
    "weighted_price": "加权平均单价",
    "weighted_avg_price": "加权平均单价",
    "weighted_unit_price": "加权平均单价",
    "avg_price": "加权平均单价",
    "outbound_amount": "出库金额",
    "out_amount": "出库金额",
    "outbound_value": "出库金额",
    "ending_amount": "期末金额",
    "ending_qty": "期末数量",
    "turnover_days": "库存周转天数",
    "inventory_turnover_days": "库存周转天数",
    "stock_turnover_days": "库存周转天数",
}

# 按完成率档位涂色(对应公式说明 §2.4 阶梯提成)
COMPLETION_TIERS: list[tuple[float, str]] = [
    (0.8, "FFCCCC"),  # 未达标 (<80%)  红
    (1.0, "FFEECC"),  # 基本达标 (80-100%)  橙
    (1.2, "DDFFDD"),  # 超额 (100-120%)  浅绿
    (float("inf"), "AAEEAA"),  # 高位 (≥120%)  深绿
]

# 回款率档位
COLLECTION_TIERS: list[tuple[float, str]] = [
    (0.70, "FFCCCC"),  # 低
    (0.85, "FFEECC"),  # 中
    (float("inf"), "DDFFDD"),  # 高
]

# 库存周转天数档位(对应 docx §3.2 健康度表)—— 越低越好,所以颜色反向
TURNOVER_DAYS_TIERS: list[tuple[float, str]] = [
    (30.0, "AAEEAA"),   # 良好  深绿
    (60.0, "DDFFDD"),   # 一般  浅绿
    (90.0, "FFEECC"),   # 偏慢  橙
    (float("inf"), "FFCCCC"),  # 滞销  红
]

TIER_MAP_BY_COLNAME: dict[str, list[tuple[float, str]]] = {
    "目标完成率": COMPLETION_TIERS,
    "回款率": COLLECTION_TIERS,
    "库存周转天数": TURNOVER_DAYS_TIERS,
}


# 库存健康度(对应 docx §3.2)—— 由 R 值映射
def _health_label_for_turnover(r: float) -> str:
    if r < 30:
        return "良好"
    if r < 60:
        return "一般"
    if r < 90:
        return "偏慢"
    return "滞销"


# ABC 分类阈值(对应 docx §4.1 帕累托法)
ABC_THRESHOLDS = (0.80, 0.95)  # A: 0-80%, B: 80-95%, C: 95-100%


def _assign_abc_labels(records: list[dict], value_col: str) -> list[dict]:
    """对 records 按 value_col 降序累计百分比,打 A/B/C 标签(就地修改并返回)。"""
    if not records or value_col not in records[0]:
        return records
    sorted_recs = sorted(records, key=lambda r: r.get(value_col, 0), reverse=True)
    total = sum(r[value_col] for r in sorted_recs if isinstance(r[value_col], (int, float)))
    if total <= 0:
        return records
    cumulative = 0.0
    for r in sorted_recs:
        cumulative += r[value_col]
        pct = cumulative / total
        if pct < ABC_THRESHOLDS[0]:
            r["ABC分类"] = "A"
        elif pct < ABC_THRESHOLDS[1]:
            r["ABC分类"] = "B"
        else:
            r["ABC分类"] = "C"
    return records

# 命中其中之一 → 列应用 0.00% 百分比格式
PERCENT_HINT_KEYWORDS = ("完成率", "回款率", "增长率", "贡献率", "_rate", "rate", "比例")


# 销售大区 → 色板(7 个大区每个一色)
REGION_COLORS: dict[str, str] = {
    "华东大区": "4F81BD",
    "华南大区": "C0504D",
    "华北大区": "9BBB59",
    "华中大区": "8064A2",
    "西南大区": "4BACC6",
    "西北大区": "F79646",
    "东北大区": "8C8C8C",
}
DEFAULT_REGION_COLOR = "BFBFBF"

# 通用 7 色板(给非预定义分组维度用,如仓库 / 类别 / 供应商)
PALETTE_7: list[str] = ["4F81BD", "C0504D", "9BBB59", "8064A2", "4BACC6", "F79646", "8C8C8C"]

# 候选分组列(按优先级匹配 metadata 里第一个存在的列)
GROUP_DIMENSION_CANDIDATES: list[str] = [
    "销售大区",
    "存放仓库",
    "物料类别",
    "主要供应商",
    "产品线",
]


def _pick_group_dimension(record_keys: list[str]) -> Optional[str]:
    for candidate in GROUP_DIMENSION_CANDIDATES:
        if candidate in record_keys:
            return candidate
    return None


def _color_for_value(value: Any, palette_index: dict[str, int]) -> str:
    """同一 group dim 内,稳定地把每个 value 映射到一种颜色。"""
    if value in REGION_COLORS:
        return REGION_COLORS[value]
    if value not in palette_index:
        palette_index[value] = len(palette_index) % len(PALETTE_7)
    return PALETTE_7[palette_index[value]]


# 不同 rate 类型的 KPI 元数据(达标阈值 + 文案)
KPI_RULES_BY_COLNAME: dict[str, dict] = {
    "目标完成率": {
        "average_label": "平均完成率",
        "average_subtitle": "全部订单按行等权均值",
        "threshold": 1.0,
        "threshold_label": "达标率",
        "threshold_subtitle_fmt": "达标 {hit} / 总数 {total}(≥100%)",
        "top_label": "TOP 3 完成率",
        "bottom_label": "待提升 BOTTOM 3",
    },
    "回款率": {
        "average_label": "平均回款率",
        "average_subtitle": "全部订单按行等权均值",
        "threshold": 0.85,
        "threshold_label": "高回款率",
        "threshold_subtitle_fmt": "≥85% 共 {hit} / {total} 笔",
        "top_label": "TOP 3 回款率",
        "bottom_label": "待催收 BOTTOM 3",
    },
    # 库存场景 —— 货币量,无固定阈值 → KPI 2 用"超均值占比"
    "加权平均单价": {
        "average_label": "综合加权单价",
        "average_subtitle": "按 SKU 等权均值",
        "threshold": None,  # None → 运行时取均值动态阈值
        "threshold_label": "高于均价 SKU 比",
        "threshold_subtitle_fmt": "高于均价 {hit} / {total} 项",
        "top_label": "TOP 3 高单价 SKU",
        "bottom_label": "BOTTOM 3 低单价 SKU",
        "value_kind": "currency",
    },
    "出库金额": {
        "average_label": "平均出库金额",
        "average_subtitle": "按 SKU 等权均值",
        "threshold": None,
        "threshold_label": "高出库 SKU 比",
        "threshold_subtitle_fmt": "高于均值 {hit} / {total} 项",
        "top_label": "TOP 3 出库金额",
        "bottom_label": "BOTTOM 3 出库金额",
        "value_kind": "currency",
    },
    "库存周转天数": {
        "average_label": "平均周转天数",
        "average_subtitle": "按 SKU 等权均值",
        "threshold": 90.0,
        "threshold_label": "滞销 SKU 比",
        "threshold_subtitle_fmt": "≥90 天共 {hit} / {total} 项",
        "top_label": "TOP 3 周转最慢",
        "bottom_label": "TOP 3 周转最快",
        "value_kind": "number",
        "value_decimals": 1,  # 天数只保留 1 位小数
    },
}


def _looks_like_percent_column(name: str) -> bool:
    n = name.lower()
    return any(k.lower() in n for k in PERCENT_HINT_KEYWORDS)


def _pick_cell_format(result_col_name: str) -> Optional[str]:
    """
    根据列名自动选 openpyxl number_format。
    优先级:百分比 > 天数 > 货币(金额/单价)> KPI rule value_kind > 默认无格式。

    所有 sheet(明细 / 大区/仓库汇总 / TOP10 / BOTTOM10 / 呆滞)共用这个选择,
    避免出现"明细页 1 位小数,排行榜 6 位小数"的不一致。
    """
    rule = KPI_RULES_BY_COLNAME.get(result_col_name) or {}
    decimals = rule.get("value_decimals", 2)

    # 1. 百分比
    if _looks_like_percent_column(result_col_name):
        return "0.00%"

    # 2. 天数(默认 1 位)
    if "天数" in result_col_name or "周转" in result_col_name:
        n = rule.get("value_decimals", 1)
        return "0" if n == 0 else f"0.{'0' * n}"

    # 3. 货币
    if rule.get("value_kind") == "currency" or "金额" in result_col_name or "单价" in result_col_name:
        return f"¥#,##0.{'0' * decimals}"

    # 4. KPI rule 指定 number
    if rule.get("value_kind") == "number":
        return "0" if decimals == 0 else f"0.{'0' * decimals}"

    return None


def _render_row_aligned_rich(
    *,
    decrypted: list,
    metadata_rows: list,
    metadata_columns: Optional[list[str]],
    primary_spec: SheetSpec,
) -> list[SheetData]:
    """
    row-aligned 解密结果 + 明文标识列 → **4 sheet 产品级输出**:
    - Sheet 1 明细     :100 行 + 顶部 4 张 KPI 卡 + 档位涂色,无大图
    - Sheet 2 大区汇总 :按大区聚合 + 大区色块柱状图
    - Sheet 3 TOP 10  :前 10 名 + 大区色块柱状图
    - Sheet 4 BOTTOM 10:后 10 名 + 大区色块柱状图
    """
    cols_in = metadata_columns or list(metadata_rows[0].keys())

    # === 1. 解密值列名,中文化 ===
    result_col_name = "结果"
    if primary_spec.columns:
        extras = [c for c in primary_spec.columns if c not in cols_in]
        if extras:
            result_col_name = extras[-1]
    result_col_name = EN_TO_ZH_COLUMN_NAMES.get(result_col_name.lower(), result_col_name)
    # 集中选择 cell number_format(所有 sheet 共用)
    pct_fmt: Optional[str] = _pick_cell_format(result_col_name)

    # === 2. 列顺序调整:让"分组维度"紧贴在"主体名称"之前 ===
    cols_out = list(cols_in)
    group_col = _pick_group_dimension(cols_out)
    name_candidates = ("销售代表", "物料名称")
    name_col = next((c for c in name_candidates if c in cols_out), None)
    if group_col and name_col and group_col != name_col:
        cols_out.remove(group_col)
        idx = cols_out.index(name_col)
        cols_out.insert(idx, group_col)

    # === 3. 组装 records(原始 list of dict + rate)用于后续多次使用 ===
    records: list[dict] = []
    for i, meta in enumerate(metadata_rows):
        rec = {c: meta.get(c) for c in cols_out}
        rec[result_col_name] = decrypted[i]
        records.append(rec)

    # === 4. Sheet 1:明细(KPI 卡 + 100 行 + 档位涂色,无大图)===
    detail_sheet = _build_detail_sheet(
        records=records,
        cols_out=cols_out,
        result_col_name=result_col_name,
        pct_fmt=pct_fmt,
        primary_spec=primary_spec,
    )

    # === 5. Sheet 2:大区汇总 ===
    region_sheet = _build_region_summary_sheet(
        records=records, result_col_name=result_col_name, pct_fmt=pct_fmt
    )

    # === 6. Sheet 3 / 4:Top10 / Bottom10 ===
    top_sheet = _build_ranking_sheet(
        records=records,
        result_col_name=result_col_name,
        pct_fmt=pct_fmt,
        n=10,
        ascending=False,
        sheet_name=f"TOP10 {result_col_name}",
    )
    bottom_sheet = _build_ranking_sheet(
        records=records,
        result_col_name=result_col_name,
        pct_fmt=pct_fmt,
        n=10,
        ascending=True,
        sheet_name=f"BOTTOM10 {result_col_name}",
    )

    sheets = [detail_sheet, region_sheet, top_sheet, bottom_sheet]

    # 业务领域特定的增强 sheet
    if result_col_name == "出库金额":
        abc_sheet = _build_abc_summary_sheet(records=records, result_col_name=result_col_name)
        if abc_sheet:
            sheets.append(abc_sheet)
    if result_col_name == "库存周转天数":
        dormant_sheet = _build_dormant_sheet(records=records, result_col_name=result_col_name)
        if dormant_sheet:
            sheets.append(dormant_sheet)

    return [s for s in sheets if s is not None]


def _fmt_pct(v: float) -> str:
    """0.1234 → '12.34%'。"""
    return f"{v * 100:.2f}%"


def _fmt_currency(v: float) -> str:
    """金额格式:¥123,456.78。"""
    return f"¥{v:,.2f}"


def _make_value_formatter(rule: dict, result_col_name: str):
    """根据 value_kind 决定标量怎么格式化。"""
    kind = rule.get("value_kind", "percent")
    decimals = rule.get("value_decimals", 2)
    if kind == "currency":
        return lambda v: f"¥{v:,.{decimals}f}"
    if kind == "number":
        return lambda v: f"{v:,.{decimals}f}"
    # 默认百分比(对应原有率类指标)
    return _fmt_pct


def _build_kpi_cards(records: list[dict], result_col_name: str) -> list[KpiCard]:
    """根据 result_col_name 选 KPI 模板,从 records 计算 4 张卡片。"""
    rule = KPI_RULES_BY_COLNAME.get(result_col_name)
    if not rule or not records:
        return []
    vals = [r[result_col_name] for r in records if isinstance(r.get(result_col_name), (int, float))]
    n = len(vals)
    if n == 0:
        return []
    mean_v = sum(vals) / n
    # threshold = None → 动态阈值取均值
    threshold = rule.get("threshold")
    if threshold is None:
        threshold = mean_v
    hit = sum(1 for v in vals if v >= threshold)
    sorted_recs = sorted(records, key=lambda r: r[result_col_name], reverse=True)
    top3 = sorted_recs[:3]
    bottom3 = sorted_recs[-3:][::-1]  # 倒序,最差排第一

    fmt_val = _make_value_formatter(rule, result_col_name)

    def _name(rec: dict) -> str:
        # 销售场景用销售代表;库存场景用物料名称/编码
        return (
            rec.get("销售代表")
            or rec.get("员工编号")
            or rec.get("物料名称")
            or rec.get("物料编码")
            or "?"
        )

    # KPI 2 的命中率展示(百分比指标用百分比文案,货币指标用"高于均值"文案)
    if rule.get("value_kind") in ("currency", "number"):
        kpi2_value = f"{hit / n * 100:.1f}%"
    else:
        kpi2_value = f"{hit / n * 100:.1f}%"

    return [
        KpiCard(
            label=rule["average_label"],
            value=fmt_val(mean_v),
            subtitle=rule["average_subtitle"],
            bg_color="EAF1F8",
        ),
        KpiCard(
            label=rule["threshold_label"],
            value=f"{hit / n * 100:.1f}%",
            subtitle=rule["threshold_subtitle_fmt"].format(hit=hit, total=n),
            bg_color="E8F5E9",
        ),
        KpiCard(
            label=rule["top_label"],
            value="\n".join(f"{_name(r)} {fmt_val(r[result_col_name])}" for r in top3),
            subtitle="按指标降序",
            bg_color="E3F2FD",
            value_size=12,
        ),
        KpiCard(
            label=rule["bottom_label"],
            value="\n".join(f"{_name(r)} {fmt_val(r[result_col_name])}" for r in bottom3),
            subtitle="按指标升序",
            bg_color="FFEBEE",
            value_size=12,
        ),
    ]


def _build_detail_sheet(
    *,
    records: list[dict],
    cols_out: list[str],
    result_col_name: str,
    pct_fmt: Optional[str],
    primary_spec: SheetSpec,
) -> SheetData:
    """
    明细 sheet:KPI + 100 行表 + 业务列(ABC 分类 / 健康度)。

    业务增强:
    - result=出库金额  → 自动加 "ABC分类" 列(按帕累托累计阈值)
    - result=库存周转天数 → 自动加 "健康度" 列(良好/一般/偏慢/滞销)
    """
    # 业务领域增强:打标签(就地修改 records,影响后续 sheets 也能拿到)
    extra_cols: list[str] = []
    if result_col_name == "出库金额":
        _assign_abc_labels(records, value_col=result_col_name)
        extra_cols.append("ABC分类")
    if result_col_name == "库存周转天数":
        for r in records:
            v = r.get(result_col_name)
            if isinstance(v, (int, float)):
                r["健康度"] = _health_label_for_turnover(v)
        extra_cols.append("健康度")

    headers = cols_out + [result_col_name] + extra_cols

    keys = list(records[0].keys()) if records else []
    group_col = _pick_group_dimension(keys)
    name_col = next((c for c in ("销售代表", "物料名称") if c in keys), None)
    if group_col or name_col:
        sorted_recs = sorted(
            records,
            key=lambda r: (
                str(r.get(group_col) or "") if group_col else "",
                str(r.get(name_col) or "") if name_col else "",
            ),
        )
    else:
        sorted_recs = sorted(records, key=lambda r: r[result_col_name], reverse=True)
    rows = [
        [r.get(c) for c in cols_out] + [r[result_col_name]] + [r.get(c) for c in extra_cols]
        for r in sorted_recs
    ]

    number_formats: dict[str, str] = {}
    if pct_fmt:
        number_formats[result_col_name] = pct_fmt

    tier_colors: dict[str, list[tuple[float, str]]] = {}
    if result_col_name in TIER_MAP_BY_COLNAME:
        tier_colors[result_col_name] = TIER_MAP_BY_COLNAME[result_col_name]

    return SheetData(
        name=f"{result_col_name}明细"[:31],
        headers=headers,
        rows=rows,
        chart=None,
        number_formats=number_formats,
        tier_colors=tier_colors,
        kpi_cards=_build_kpi_cards(sorted_recs, result_col_name),
    )


def _build_abc_summary_sheet(*, records: list[dict], result_col_name: str) -> Optional[SheetData]:
    """ABC 分类汇总(对应 docx §4.2)。"""
    if not records or "ABC分类" not in records[0]:
        return None
    classes: dict[str, list[dict]] = {"A": [], "B": [], "C": []}
    for r in records:
        cls = r.get("ABC分类")
        if cls in classes:
            classes[cls].append(r)
    total = sum(r[result_col_name] for r in records if isinstance(r[result_col_name], (int, float)))
    if total <= 0:
        return None

    headers = ["分类", "SKU 数", "出库金额合计", "金额占比", "管理建议"]
    rows = []
    advice_map = {
        "A": "关键品 · 严格控制 · 低安全库存按需补货",
        "B": "次重要 · 一般控制 · 定期复盘",
        "C": "普通品 · 放松控制 · 大批量低频补货",
    }
    for cls in ("A", "B", "C"):
        items = classes[cls]
        amount_sum = sum(r[result_col_name] for r in items)
        rows.append([cls, len(items), amount_sum, amount_sum / total, advice_map[cls]])

    number_formats = {
        "出库金额合计": "¥#,##0.00",
        "金额占比": "0.00%",
    }
    # 用 ABC 三色:A 红重点,B 橙,C 灰
    abc_tier = {
        "ABC分类": [],  # 不用 tier_colors 直接对分类列染色,这里跳过;留给手动 fill
    }
    chart = ChartSpec(type="bar", x="分类", y="出库金额合计", title="ABC 分类出库金额合计")
    bar_colors = ["C0504D", "F79646", "9BBB59"]  # A=红 B=橙 C=绿

    return SheetData(
        name=f"ABC 分类汇总-{result_col_name}"[:31],
        headers=headers,
        rows=rows,
        chart=chart,
        number_formats=number_formats,
        series_colors_by_row=bar_colors,
    )


def _extend_month_labels(dates: list[str], horizon: int) -> list[str]:
    """从历史日期(YYYY-MM)往后延伸 horizon 个月。"""
    parsed = []
    for d in dates:
        s = str(d)
        parts = s.split("-")
        if len(parts) >= 2:
            try:
                parsed.append((int(parts[0]), int(parts[1])))
            except (TypeError, ValueError):
                return [f"+{i+1}" for i in range(horizon)]
        else:
            return [f"+{i+1}" for i in range(horizon)]
    if not parsed:
        return [f"+{i+1}" for i in range(horizon)]
    y, m = parsed[-1]
    out = []
    for _ in range(horizon):
        m += 1
        if m > 12:
            m = 1
            y += 1
        out.append(f"{y:04d}-{m:02d}")
    return out


def _compute_forecasts(history: list[float], horizon: int, methods: list[str]) -> dict[str, list[float]]:
    """
    对历史时间序列计算多种预测方法,各延伸 horizon 步。

    支持:
    - MA3 / MA6 ... MA{N}    : 简单移动平均(last N)
    - WMA                   : 加权移动平均(权重 0.5/0.3/0.2,最近权重最大)
    - OLS                   : 一阶线性回归 y = a*t + b 外推
    - EWMA                  : 指数加权移动平均(alpha=0.4)
    """
    import math

    n = len(history)
    if n == 0:
        return {m: [0.0] * horizon for m in methods}

    out: dict[str, list[float]] = {}
    for m in methods:
        if m.startswith("MA") and m[2:].isdigit():
            k = min(int(m[2:]), n)
            val = sum(history[-k:]) / k
            out[m] = [val] * horizon
        elif m == "WMA":
            # 老→新 权重递增(最新月份权重最大)
            w = [0.2, 0.3, 0.5]
            k = min(len(w), n)
            ws = w[-k:]  # 取末尾 k 个权重,对应最近 k 个月
            vs = history[-k:]
            tot_w = sum(ws)
            val = sum(a * b for a, b in zip(ws, vs)) / tot_w
            out[m] = [val] * horizon
        elif m == "OLS":
            # y = a*t + b 最小二乘
            t = list(range(1, n + 1))
            mean_t = sum(t) / n
            mean_y = sum(history) / n
            num = sum((ti - mean_t) * (yi - mean_y) for ti, yi in zip(t, history))
            den = sum((ti - mean_t) ** 2 for ti in t)
            a = num / den if den else 0.0
            b = mean_y - a * mean_t
            out[m] = [a * (n + i) + b for i in range(1, horizon + 1)]
        elif m == "EWMA":
            alpha = 0.4
            s = history[0]
            for v in history[1:]:
                s = alpha * v + (1 - alpha) * s
            out[m] = [s] * horizon
        elif m == "ETS":
            # Holt 双指数平滑(level + trend,无季节性)
            # L_t = α·Y_t + (1−α)·(L_{t−1} + T_{t−1})
            # T_t = β·(L_t − L_{t−1}) + (1−β)·T_{t−1}
            # F_{t+h} = L_t + h·T_t
            if n < 2:
                out[m] = [h[-1] if h else 0.0 for h in [history]] * horizon
                continue
            alpha, beta = 0.4, 0.2
            L = history[0]
            T = history[1] - history[0]
            for t in range(1, n):
                L_prev = L
                L = alpha * history[t] + (1 - alpha) * (L_prev + T)
                T = beta * (L - L_prev) + (1 - beta) * T
            out[m] = [L + (j + 1) * T for j in range(horizon)]
        elif m == "ARIMA":
            # ARIMA(1,1,0):一阶差分 + AR(1)
            # d_t = Y_t − Y_{t−1}, d_t = c + φ·d_{t−1} + ε_t
            # 然后逆差分还原
            if n < 3:
                out[m] = [history[-1]] * horizon
                continue
            diffs = [history[i] - history[i - 1] for i in range(1, n)]
            if len(diffs) < 2:
                out[m] = [history[-1]] * horizon
                continue
            x = diffs[:-1]
            y = diffs[1:]
            mx = sum(x) / len(x)
            my = sum(y) / len(y)
            num = sum((xi - mx) * (yi - my) for xi, yi in zip(x, y))
            den = sum((xi - mx) ** 2 for xi in x)
            phi = num / den if den else 0.0
            phi = max(-0.95, min(0.95, phi))  # 限制避免爆炸
            c = my - phi * mx
            last_diff = diffs[-1]
            last_y = history[-1]
            preds = []
            for _ in range(horizon):
                next_diff = c + phi * last_diff
                last_y = last_y + next_diff
                preds.append(last_y)
                last_diff = next_diff
            out[m] = preds
        else:
            # 未知方法:用整体均值兜底
            out[m] = [sum(history) / n] * horizon
    return out


METHOD_LABELS_ZH = {
    "MA3": "MA3 移动平均",
    "MA6": "MA6 移动平均",
    "WMA": "加权移动平均",
    "OLS": "线性回归外推",
    "EWMA": "指数加权",
    "ETS": "Holt 双指数平滑",
    "ARIMA": "ARIMA(1,1,0)",
}

METHOD_COLORS = {
    "实际值": "1F4E79",  # 深蓝
    "MA3": "9BBB59",  # 绿
    "MA6": "4BACC6",  # 青
    "WMA": "F79646",  # 橙
    "OLS": "C0504D",  # 红
    "EWMA": "8064A2",  # 紫
    "ETS": "00B0F0",  # 亮蓝(自适应趋势)
    "ARIMA": "B85FBC",  # 玫红(随机过程模型)
}


def _apply_seasonal_to_forecast(
    forecast_values: list[float],
    seasonal_factors: list[float],
    horizon: int,
    historical_dates: Optional[list[str]] = None,
    future_dates: Optional[list[str]] = None,
) -> list[float]:
    """
    将基础 OLS 预测乘以"未来月份的季节因子"。

    正确策略:把历史 季节因子 按 month-of-year(YYYY-MM 末两位)建索引;
    未来月份去查同 month-of-year 的因子;查不到用 1.0(中性)。
    """
    n_hist = len(seasonal_factors)
    if n_hist == 0:
        return forecast_values

    # 构造 month-of-year → factor 映射(同月有多年取最近一次)
    mm_to_sf: dict[str, float] = {}
    if historical_dates and len(historical_dates) == n_hist:
        for d, sf in zip(historical_dates, seasonal_factors):
            mm = str(d)[-2:]
            mm_to_sf[mm] = sf

    adjusted = []
    for j in range(horizon):
        sf = 1.0
        if future_dates and j < len(future_dates) and mm_to_sf:
            mm = str(future_dates[j])[-2:]
            sf = mm_to_sf.get(mm, 1.0)
        else:
            # 兜底:循环用历史最后 horizon 期
            sf = seasonal_factors[(n_hist + j) % n_hist] if n_hist else 1.0
        adjusted.append(forecast_values[j] * (sf or 1.0))
    return adjusted


def _compute_confidence_interval(
    historical: list[float],
    forecast_values: list[float],
    z: float = 1.96,
) -> tuple[list[float], list[float]]:
    """
    基于历史波动率给出预测的 ±z·σ 上下界。
    返回 (upper, lower) 各 len(forecast_values)。
    """
    n = len(historical)
    if n < 2:
        return forecast_values, forecast_values
    mean_h = sum(historical) / n
    var_h = sum((v - mean_h) ** 2 for v in historical) / (n - 1)
    sigma = var_h**0.5
    upper = [f + z * sigma for f in forecast_values]
    lower = [max(0.0, f - z * sigma) for f in forecast_values]
    return upper, lower


def _compute_yoy_per_row(
    historical_with_dates: list[tuple[str, float]],
    forecast_with_dates: list[tuple[str, float]],
) -> list[Optional[float]]:
    """
    对每个 forecast 月份找去年同月(月份字符串末两位匹配)算 YoY。
    返回与 forecast_with_dates 等长的 list[Optional[float]],无对应历史的格为 None。
    """
    hist_by_mm = {}
    for d, v in historical_with_dates:
        mm = str(d)[-2:]
        # 同 month-of-year 取最近一次
        hist_by_mm.setdefault(mm, []).append((d, v))
    out = []
    for d, f in forecast_with_dates:
        mm = str(d)[-2:]
        if mm in hist_by_mm and hist_by_mm[mm]:
            base = hist_by_mm[mm][-1][1]
            out.append((f - base) / base if base else None)
        else:
            out.append(None)
    return out


# 产品线 / 大区 列名约定:总览列 + 维度前缀
DIM_LINE_PREFIX = "line_"
DIM_REGION_PREFIX = "region_"
TOTAL_COL_NAMES = ("total_sales", "total", "all_sales", "全公司销售额")

# 维度列名 英 → 中(避免在 Excel 看到 line_industrial / region_east)
LINE_EN_TO_ZH: dict[str, str] = {
    "industrial": "工控平台", "comm": "通信", "hmi": "人机界面",
    "spare": "备品备件", "mvision": "机器视觉", "sense": "感知设备",
    "motion": "运动控制", "power": "电源",
}
REGION_EN_TO_ZH: dict[str, str] = {
    "east": "华东大区", "central": "华中大区", "north": "华北大区",
    "south": "华南大区", "southwest": "西南大区", "overseas": "海外",
}


def _render_forecast_sheets(
    *,
    historical: list[float],
    metadata_rows: list[dict],
    metadata_columns: Optional[list[str]],
    primary_spec: SheetSpec,
    horizon: int,
    methods: list[str],
) -> list[SheetData]:
    """
    时间序列预测渲染:
    - Sheet 1 "预测对比":历史 + 4 种方法各延伸 horizon 步,折线图同时展示
    - Sheet 2 "预测汇总":每种方法的 horizon 期总和 / 均值 / vs 历史最近 N
    """
    # 找日期列(metadata 里第一个 YYYY-MM 模式的列)
    date_col = next(
        (c for c in (metadata_columns or []) if c in ("销售月份", "月份", "date", "month")),
        None,
    )
    if not date_col:
        date_col = (metadata_columns or [list(metadata_rows[0].keys())[0]])[0]

    dates = [str(r.get(date_col)) for r in metadata_rows]
    future_dates = _extend_month_labels(dates, horizon)

    forecasts = _compute_forecasts(historical, horizon, methods)

    # === 增强 1:季节因子调整(若 metadata 含季节因子列)===
    seasonal_col = next(
        (c for c in (metadata_columns or []) if c in ("季节因子", "seasonal_factor")), None
    )
    seasonal_factors = (
        [float(r.get(seasonal_col) or 1.0) for r in metadata_rows] if seasonal_col else []
    )
    ols_seasonal_adj = (
        _apply_seasonal_to_forecast(
            forecasts.get("OLS", []), seasonal_factors, horizon,
            historical_dates=dates, future_dates=future_dates,
        )
        if seasonal_factors and "OLS" in forecasts
        else []
    )

    # === 增强 2:置信区间 ±1.96σ(基于 OLS 预测)===
    ci_upper, ci_lower = _compute_confidence_interval(historical, forecasts.get("OLS", []))

    # === 增强 3:YoY 同比增长率(forecast 月 vs 同名 month-of-year 的历史)===
    hist_with_dates = list(zip(dates, historical))
    fcst_with_dates = list(zip(future_dates, forecasts.get("OLS", [])))
    yoy_per_forecast = _compute_yoy_per_row(hist_with_dates, fcst_with_dates)

    # === Sheet 1:预测对比(扩展)===
    actual_label = "实际销售额"
    method_cols = [METHOD_LABELS_ZH.get(m, m) for m in methods]
    extra_cols: list[str] = []
    if ols_seasonal_adj:
        extra_cols.append("OLS 季节调整")
    extra_cols += ["OLS 上界 +1.96σ", "OLS 下界 -1.96σ", "YoY 增长率"]

    headers = [date_col, actual_label] + method_cols + extra_cols
    rows: list[list] = []

    # 历史部分:仅实际值,extra 列空
    for i, d in enumerate(dates):
        rows.append([d, historical[i]] + [None] * len(methods) + [None] * len(extra_cols))
    # 预测部分:各方法 + 增强列
    for j, d in enumerate(future_dates):
        row = [d, None]
        for m in methods:
            row.append(forecasts[m][j])
        if ols_seasonal_adj:
            row.append(ols_seasonal_adj[j])
        row.append(ci_upper[j] if ci_upper else None)
        row.append(ci_lower[j] if ci_lower else None)
        row.append(yoy_per_forecast[j])
        rows.append(row)

    # 主图:仅画核心 5 条曲线(避免太挤);上下界用单独标记
    chart = ChartSpec(
        type="line",
        x=date_col,
        y=[actual_label] + method_cols,
        title=f"销售额历史 + {horizon} 个月多方法预测对比",
    )
    cell_fmt = "¥#,##0.00"
    number_formats = {h: cell_fmt for h in headers if h not in (date_col, "YoY 增长率")}
    number_formats["YoY 增长率"] = "0.00%"

    detail_sheet = SheetData(
        name="销售预测对比",
        headers=headers,
        rows=rows,
        chart=chart,
        number_formats=number_formats,
    )

    # === Sheet 2:预测汇总 KPI ===
    summary_headers = ["预测方法", f"未来 {horizon} 期合计", f"未来 {horizon} 期均值", "vs 历史均值"]
    historical_mean = sum(historical) / len(historical) if historical else 0
    summary_rows = []
    for m in methods:
        future_vals = forecasts[m]
        f_sum = sum(future_vals)
        f_mean = f_sum / len(future_vals) if future_vals else 0
        delta = (f_mean - historical_mean) / historical_mean if historical_mean else 0
        summary_rows.append([METHOD_LABELS_ZH.get(m, m), f_sum, f_mean, delta])

    summary_sheet = SheetData(
        name="预测方法汇总",
        headers=summary_headers,
        rows=summary_rows,
        chart=ChartSpec(
            type="bar",
            x="预测方法",
            y=f"未来 {horizon} 期均值",
            title=f"4 种方法未来 {horizon} 期均值对比",
        ),
        number_formats={
            f"未来 {horizon} 期合计": "¥#,##0.00",
            f"未来 {horizon} 期均值": "¥#,##0.00",
            "vs 历史均值": "0.00%",
        },
    )

    # KPI 卡(顶部 4 张):历史均值 / OLS 预测均值 / 最乐观方法 / 最保守方法
    kpi_cards = []
    method_means = [(m, sum(forecasts[m]) / len(forecasts[m])) for m in methods]
    method_means_sorted = sorted(method_means, key=lambda x: x[1], reverse=True)
    most_opt = method_means_sorted[0] if method_means_sorted else None
    most_pess = method_means_sorted[-1] if method_means_sorted else None
    kpi_cards = [
        KpiCard(
            label="历史均值",
            value=f"¥{historical_mean:,.0f}",
            subtitle=f"过去 {len(historical)} 个月",
            bg_color="EAF1F8",
        ),
        KpiCard(
            label="OLS 趋势预测均值",
            value=f"¥{(sum(forecasts.get('OLS', [0])) / max(1, len(forecasts.get('OLS', [0])))):,.0f}",
            subtitle=f"未来 {horizon} 个月线性外推",
            bg_color="E8F5E9",
        ),
        KpiCard(
            label="最乐观",
            value=f"{METHOD_LABELS_ZH.get(most_opt[0], most_opt[0]) if most_opt else '-'}\n¥{most_opt[1]:,.0f}" if most_opt else "-",
            subtitle="预测均值最高",
            bg_color="E3F2FD",
            value_size=12,
        ),
        KpiCard(
            label="最保守",
            value=f"{METHOD_LABELS_ZH.get(most_pess[0], most_pess[0]) if most_pess else '-'}\n¥{most_pess[1]:,.0f}" if most_pess else "-",
            subtitle="预测均值最低",
            bg_color="FFEBEE",
            value_size=12,
        ),
    ]
    detail_sheet.kpi_cards = kpi_cards

    return [detail_sheet, summary_sheet]


def _render_forecast_sheets_multi(
    *,
    df,  # pandas.DataFrame,各列是 total_sales / line_* / region_*
    metadata_rows: list[dict],
    metadata_columns: Optional[list[str]],
    primary_spec: SheetSpec,
    horizon: int,
    methods: list[str],
    value_col: Optional[str],
    focus_dim: str = "all",
) -> list[SheetData]:
    """
    多列时间序列预测;focus_dim 控制是否生成产品线/大区分维 sheet:
    - "all"    : 全部出(default)—— 全公司 + 8 产品线 + 6 大区
    - "line"   : 仅产品线维度(隐藏大区)
    - "region" : 仅大区维度(隐藏产品线)
    - "total"  : 仅全公司,不出任何分维 sheet
    """
    cols = list(df.columns)
    main_col = value_col if value_col and value_col in cols else next(
        (c for c in cols if c in TOTAL_COL_NAMES), cols[0]
    )
    line_cols = [c for c in cols if str(c).startswith(DIM_LINE_PREFIX)]
    region_cols = [c for c in cols if str(c).startswith(DIM_REGION_PREFIX)]

    # focus_dim 过滤
    if focus_dim == "line":
        region_cols = []
    elif focus_dim == "region":
        line_cols = []
    elif focus_dim == "total":
        line_cols, region_cols = [], []

    # 主列调用单序列路径(已含季节/CI/YoY 增强)
    main_history = [float(v) for v in df[main_col].tolist()]
    sheets = _render_forecast_sheets(
        historical=main_history,
        metadata_rows=metadata_rows,
        metadata_columns=metadata_columns,
        primary_spec=primary_spec,
        horizon=horizon,
        methods=methods,
    )

    # 找日期列 + 未来月
    date_col = next(
        (c for c in (metadata_columns or []) if c in ("销售月份", "月份", "date", "month")), None
    )
    dates = [str(r.get(date_col)) for r in metadata_rows] if date_col else [str(i) for i in range(len(df))]
    future_dates = _extend_month_labels(dates, horizon)

    # === Sheet 3:按产品线分维度并排预测 ===
    if line_cols:
        sheets.append(
            _build_dimension_forecast_sheet(
                dim_cols=line_cols,
                df=df,
                dates=dates,
                future_dates=future_dates,
                date_col=date_col or "月份",
                horizon=horizon,
                dim_label="产品线",
                prefix=DIM_LINE_PREFIX,
            )
        )

    # === Sheet 4:按大区分维度并排预测 ===
    if region_cols:
        sheets.append(
            _build_dimension_forecast_sheet(
                dim_cols=region_cols,
                df=df,
                dates=dates,
                future_dates=future_dates,
                date_col=date_col or "月份",
                horizon=horizon,
                dim_label="销售大区",
                prefix=DIM_REGION_PREFIX,
            )
        )

    # === Sheet 5:季节因子分布 ===
    seasonal_col = next(
        (c for c in (metadata_columns or []) if c in ("季节因子", "seasonal_factor")), None
    )
    if seasonal_col:
        sf_values = [float(r.get(seasonal_col) or 1.0) for r in metadata_rows]
        sheets.append(_build_seasonal_factor_sheet(dates=dates, factors=sf_values, date_col=date_col or "月份"))

    return sheets


def _build_dimension_forecast_sheet(
    *,
    dim_cols: list[str],
    df,  # pandas.DataFrame
    dates: list[str],
    future_dates: list[str],
    date_col: str,
    horizon: int,
    dim_label: str,
    prefix: str,
) -> SheetData:
    """
    按维度并排预测:每条产品线/大区一条曲线,X = 月份(历史+预测)。
    每列做 OLS 外推 horizon 步。列名英→中(line_industrial → 工控平台)。
    """
    en_to_zh = LINE_EN_TO_ZH if prefix == DIM_LINE_PREFIX else REGION_EN_TO_ZH

    def pretty(c: str) -> str:
        en = str(c).removeprefix(prefix)
        return en_to_zh.get(en, en)

    headers = [date_col] + [pretty(c) for c in dim_cols]

    # 每列对历史值算 OLS 预测
    forecasts_by_col: dict[str, list[float]] = {}
    for c in dim_cols:
        hist = [float(v) for v in df[c].tolist()]
        f = _compute_forecasts(hist, horizon, ["OLS"])["OLS"]
        forecasts_by_col[c] = hist + f

    all_dates = dates + future_dates
    rows = []
    for i, d in enumerate(all_dates):
        row = [d] + [forecasts_by_col[c][i] for c in dim_cols]
        rows.append(row)

    chart = ChartSpec(
        type="line",
        x=date_col,
        y=[pretty(c) for c in dim_cols],
        title=f"按{dim_label} OLS 预测(+{horizon} 期)",
    )
    number_formats = {h: "¥#,##0.00" for h in headers if h != date_col}

    return SheetData(
        name=f"按{dim_label}预测",
        headers=headers,
        rows=rows,
        chart=chart,
        number_formats=number_formats,
    )


def _build_seasonal_factor_sheet(
    *, dates: list[str], factors: list[float], date_col: str
) -> SheetData:
    """季节因子按月分布,折线图。"""
    headers = [date_col, "季节因子"]
    rows = [[d, f] for d, f in zip(dates, factors)]
    chart = ChartSpec(type="line", x=date_col, y="季节因子", title="季节因子月度分布")
    return SheetData(
        name="季节因子分布",
        headers=headers,
        rows=rows,
        chart=chart,
        number_formats={"季节因子": "0.00"},
    )


def _build_dormant_sheet(*, records: list[dict], result_col_name: str) -> Optional[SheetData]:
    """呆滞物料 sheet(对应 docx §6.3):R ≥ 90 的物料筛选清单。"""
    if not records:
        return None
    dormant = [r for r in records if isinstance(r.get(result_col_name), (int, float)) and r[result_col_name] >= 90]
    if not dormant:
        return None
    # 按周转天数降序
    dormant.sort(key=lambda r: r[result_col_name], reverse=True)

    base_cols = [
        c
        for c in ("物料编码", "物料名称", "物料类别", "存放仓库", "主要供应商")
        if c in dormant[0]
    ]
    headers = ["排名"] + base_cols + [result_col_name, "健康度"]
    rows = []
    for i, r in enumerate(dormant, start=1):
        rows.append(
            [i] + [r.get(c) for c in base_cols] + [r[result_col_name], r.get("健康度", "滞销")]
        )

    number_formats = {result_col_name: _pick_cell_format(result_col_name) or "0.0"}
    tier_colors = {result_col_name: TURNOVER_DAYS_TIERS}

    return SheetData(
        name=f"呆滞物料清单-{result_col_name}"[:31],
        headers=headers,
        rows=rows,
        chart=None,
        number_formats=number_formats,
        tier_colors=tier_colors,
    )


def _build_region_summary_sheet(
    *, records: list[dict], result_col_name: str, pct_fmt: Optional[str]
) -> Optional[SheetData]:
    """
    分组汇总 sheet —— 通用化:
    - 销售场景 → 按"销售大区"
    - 库存场景 → 按"存放仓库"或"物料类别"
    - 其他    → 按 GROUP_DIMENSION_CANDIDATES 中第一个存在的列
    """
    if not records:
        return None
    group_col = _pick_group_dimension(list(records[0].keys()))
    if group_col is None:
        return None

    groups: dict[Any, list[float]] = {}
    for r in records:
        groups.setdefault(r.get(group_col), []).append(r[result_col_name])

    summary = []
    for key, vals in groups.items():
        summary.append(
            {
                group_col: key,
                "条目数": len(vals),
                f"平均{result_col_name}": sum(vals) / len(vals),
                f"最高{result_col_name}": max(vals),
                f"最低{result_col_name}": min(vals),
            }
        )
    summary.sort(key=lambda r: r[f"平均{result_col_name}"], reverse=True)

    headers = [
        group_col,
        "条目数",
        f"平均{result_col_name}",
        f"最高{result_col_name}",
        f"最低{result_col_name}",
    ]
    rows = [[r[h] for h in headers] for r in summary]

    number_formats: dict[str, str] = {}
    if pct_fmt:
        for h in headers:
            if h not in (group_col, "条目数"):
                number_formats[h] = pct_fmt

    chart = ChartSpec(
        type="bar",
        x=group_col,
        y=f"平均{result_col_name}",
        title=f"按{group_col}平均{result_col_name}",
    )
    palette_index: dict[str, int] = {}
    bar_colors = [_color_for_value(r[group_col], palette_index) for r in summary]

    return SheetData(
        name=f"{group_col}汇总-{result_col_name}"[:31],
        headers=headers,
        rows=rows,
        chart=chart,
        number_formats=number_formats,
        series_colors_by_row=bar_colors,
    )


def _build_ranking_sheet(
    *,
    records: list[dict],
    result_col_name: str,
    pct_fmt: Optional[str],
    n: int,
    ascending: bool,
    sheet_name: str,
) -> Optional[SheetData]:
    """Top N / Bottom N 排行榜。bars 按大区染色。"""
    if not records:
        return None
    sorted_recs = sorted(records, key=lambda r: r[result_col_name], reverse=not ascending)
    picked = sorted_recs[:n]
    if not picked:
        return None

    # 列:排名 / 员工编号 / 销售代表 / 销售大区 / 城市 / 产品线 / 销售月份 / rate
    # 候选身份列:销售场景的 + 库存场景的
    candidate_cols = (
        "员工编号", "销售代表", "销售大区", "城市", "产品线", "销售月份",
        "物料编码", "物料名称", "物料类别", "计量单位", "存放仓库", "主要供应商",
    )
    base_cols = [c for c in candidate_cols if c in picked[0]]
    headers = ["排名"] + base_cols + [result_col_name]
    rows = []
    for i, r in enumerate(picked, start=1):
        rows.append([i] + [r.get(c) for c in base_cols] + [r[result_col_name]])

    number_formats: dict[str, str] = {}
    if pct_fmt:
        number_formats[result_col_name] = pct_fmt

    tier_colors: dict[str, list[tuple[float, str]]] = {}
    if result_col_name in TIER_MAP_BY_COLNAME:
        tier_colors[result_col_name] = TIER_MAP_BY_COLNAME[result_col_name]

    # X 轴: 优先用"销售代表"(销售场景)/ "物料名称"(库存场景)
    x_candidates = ("销售代表", "物料名称", "物料编码", "员工编号")
    x_col = next((c for c in x_candidates if c in base_cols), None)
    chart = ChartSpec(type="bar", x=x_col, y=result_col_name, title=sheet_name) if x_col else None

    # bar 染色: 按 group dim(销售大区/存放仓库/物料类别)选色
    group_col = _pick_group_dimension(list(picked[0].keys()))
    bar_colors = None
    if group_col and group_col in base_cols:
        palette_index: dict[str, int] = {}
        bar_colors = [_color_for_value(r.get(group_col), palette_index) for r in picked]

    return SheetData(
        name=sheet_name[:31],
        headers=headers,
        rows=rows,
        chart=chart,
        number_formats=number_formats,
        tier_colors=tier_colors,
        series_colors_by_row=bar_colors,
    )


def _render_to_sheets(
    decrypted: Any,
    sheet_specs: list[SheetSpec],
    scenario: Scenario,
    metadata_rows: Optional[list] = None,
    metadata_columns: Optional[list[str]] = None,
    ops: Optional[list] = None,
) -> list[SheetData]:
    """
    把解密后的结果按 sheet spec 渲染成可写入 Excel 的 SheetData。

    metadata_rows: 若提供且与 decrypted 行数对齐,自动合并到输出
    ops: plan.ops 列表;含 forecast 时走时间序列预测渲染路径
    """
    if not sheet_specs:
        sheet_specs = [SheetSpec(name="Result")]

    primary_spec = sheet_specs[0]

    # forecast op 路径:检测到 forecast → 时间序列多曲线预测 sheet
    forecast_op = None
    for op in ops or []:
        op_name = op.op if hasattr(op, "op") else op.get("op") if isinstance(op, dict) else None
        if op_name == "forecast":
            forecast_op = op
            break

    if forecast_op is not None and metadata_rows:
        op_params = (
            forecast_op.params if hasattr(forecast_op, "params") else forecast_op.get("params", {})
        ) or {}
        horizon = int(op_params.get("horizon", 3))
        methods = op_params.get("methods") or ["MA3", "MA6", "WMA", "OLS"]
        value_col = op_params.get("value_col")

        # 形态 A:list[float] = 单列时间序列(原行为)
        if (
            isinstance(decrypted, list)
            and decrypted
            and isinstance(decrypted[0], (int, float))
            and len(decrypted) == len(metadata_rows)
        ):
            return _render_forecast_sheets(
                historical=decrypted,
                metadata_rows=metadata_rows,
                metadata_columns=metadata_columns,
                primary_spec=primary_spec,
                horizon=horizon,
                methods=methods,
            )

        # 形态 B:DataFrame = 多列时间序列(主列 + 产品线/大区维度)
        try:
            import pandas as pd
        except ImportError:
            pd = None
        if pd is not None and isinstance(decrypted, pd.DataFrame) and len(decrypted) == len(metadata_rows):
            return _render_forecast_sheets_multi(
                df=decrypted,
                metadata_rows=metadata_rows,
                metadata_columns=metadata_columns,
                primary_spec=primary_spec,
                horizon=horizon,
                methods=methods,
                value_col=value_col,
                focus_dim=str(op_params.get("focus_dim", "all")).lower(),
            )

    # 情况 -1:row-aligned list + metadata → 合并为丰富表
    if (
        isinstance(decrypted, list)
        and metadata_rows
        and len(decrypted) == len(metadata_rows)
        and decrypted
        and isinstance(decrypted[0], (int, float))
    ):
        return _render_row_aligned_rich(
            decrypted=decrypted,
            metadata_rows=metadata_rows,
            metadata_columns=metadata_columns,
            primary_spec=primary_spec,
        )

    # 情况 0:pandas DataFrame(real backend 解密 CipherDataFrame 得到的)
    try:
        import pandas as pd  # type: ignore
    except ImportError:
        pd = None  # type: ignore

    if pd is not None and isinstance(decrypted, pd.DataFrame):
        df = decrypted.reset_index()
        # 索引列名兜底
        df.columns = [str(c) if c is not None and str(c) != "" else "index" for c in df.columns]
        headers = primary_spec.columns or list(df.columns)
        # 容错:headers 在 df 中不存在的列跳过
        valid_headers = [h for h in headers if h in df.columns]
        if not valid_headers:
            valid_headers = list(df.columns)
        rows = df[valid_headers].values.tolist()
        return [
            SheetData(
                name=primary_spec.name,
                headers=valid_headers,
                rows=rows,
                chart=primary_spec.chart,
            )
        ]

    # 情况 0b:dict of scalars(CipherSeries 解密结果,如 {"a": 2.0, "b": 20.0})
    if isinstance(decrypted, dict) and decrypted and all(
        not isinstance(v, (list, dict)) for v in decrypted.values()
    ):
        headers = primary_spec.columns or ["column", "value"]
        rows = [[k, v] for k, v in decrypted.items()]
        return [SheetData(name=primary_spec.name, headers=headers, rows=rows, chart=primary_spec.chart)]

    # 情况 1:list of dict(stub pandaseal 输出)
    if isinstance(decrypted, list) and decrypted and isinstance(decrypted[0], dict):
        # 重命名 __group__ → 'group'
        normalized = [
            {("group" if k == "__group__" else k): v for k, v in row.items()}
            for row in decrypted
        ]
        headers = primary_spec.columns or list(normalized[0].keys())
        rows = [[row.get(h) for h in headers] for row in normalized]
        return [
            SheetData(
                name=primary_spec.name,
                headers=headers,
                rows=rows,
                chart=primary_spec.chart,
            )
        ]

    # 情况 2:dict with labels+matrix(corrcoef 等)
    if isinstance(decrypted, dict) and "labels" in decrypted and "matrix" in decrypted:
        labels = decrypted["labels"]
        matrix = decrypted["matrix"]
        headers = ["label", *labels]
        rows = [[labels[i], *matrix[i]] for i in range(len(labels))]
        return [SheetData(name=primary_spec.name, headers=headers, rows=rows, chart=None)]

    # 情况 3:list of scalars 或 list(标量结果)
    if isinstance(decrypted, list):
        if decrypted and isinstance(decrypted[0], (int, float, str)):
            headers = primary_spec.columns or ["value"]
            rows = [[v] for v in decrypted]
            return [SheetData(name=primary_spec.name, headers=headers, rows=rows, chart=primary_spec.chart)]
        if decrypted and isinstance(decrypted[0], list):
            n_cols = len(decrypted[0])
            headers = primary_spec.columns or [f"c{i}" for i in range(n_cols)]
            return [SheetData(name=primary_spec.name, headers=headers, rows=decrypted, chart=primary_spec.chart)]

    # 情况 4:标量(均值、求和等)
    if isinstance(decrypted, (int, float, str)):
        return [
            SheetData(
                name=primary_spec.name,
                headers=["metric", "value"],
                rows=[["result", decrypted]],
            )
        ]

    # 兜底:JSON 化展示
    return [
        SheetData(
            name=primary_spec.name,
            headers=["raw_json"],
            rows=[[json.dumps(decrypted, ensure_ascii=False, default=str)]],
        )
    ]


# ===========================================================================
# 条件路由函数
# ===========================================================================


def _after_validate_plan(state: AgentState) -> str:
    return "filter_summary" if not state.get("error") else "retry_or_fail"


def _after_filter_summary(state: AgentState, max_retries: int) -> str:
    """filter_summary clean → 直接按场景路由(不再先走 authorize)。"""
    if not state.get("summary_filter_hit"):
        return _route_scenario(state)  # 直接复用场景路由
    if state.get("retry_count", 0) < max_retries:
        return "retry_llm"
    return "fallback_summary"


def _route_scenario(state: AgentState) -> str:
    """filter_summary / fallback_summary 之后,按 scenario 路由到对应计算节点。"""
    if state.get("error"):
        return "end_early"
    plan = state.get("computation_plan", {})
    sc = plan.get("scenario")
    return {
        1: "compute_pandaseal",
        2: "compute_henumpy",
        3: "compute_helearn",
        4: "compute_hetorch",
        6: "compute_pipeline",
    }.get(sc, "end_early")  # 场景 5 不该走到这


def _after_authorize(state: AgentState) -> str:
    """authorize → decrypt(放行)或 write_excel_encrypted(拒绝);上游 error 直接 END。"""
    if state.get("error"):
        return "end_early"
    return "decrypt" if state.get("authorized") else "write_excel_encrypted"


def _retry_increment(state: AgentState) -> dict:
    """重试节点 —— 仅 +1 计数,清掉上一轮的 hit / error。"""
    return {
        "retry_count": state.get("retry_count", 0) + 1,
        "summary_filter_hit": False,
        "error": None,
    }


def _fallback_summary_node(state: AgentState) -> dict:
    """超过重试上限,使用兜底范式回复。"""
    return {
        "summary_filtered": FALLBACK_SUMMARY,
        "summary_filter_hit": False,
        "error": None,
    }


def _serialize_cipher(c) -> str:
    """密文对象 → 可写入 Excel 的字符串。

    CipherArray / CipherDataFrame cell 等的 str() 形式为 `[1.7e-77, 48.05]`,
    即 crypto_toolkit 在 encrypt_csv 中使用的密文表示(两个浮点 coefficient)。
    """
    try:
        s = str(c)
        return s if s else f"<encrypted:{type(c).__name__}>"
    except Exception:
        return f"<encrypted:{type(c).__name__}>"


def _render_encrypted_to_sheets(
    encrypted: Any,
    sheet_specs: list[SheetSpec],
    scenario: Scenario,
) -> list[SheetData]:
    """
    把**密文结果**(未解密)按 sheet spec 渲染成可写入 Excel 的 SheetData。
    各 cell 是序列化后的密文字符串;不渲染图表(数据是密文)。
    """
    if not sheet_specs:
        sheet_specs = [SheetSpec(name="EncryptedResult")]
    primary_spec = sheet_specs[0]
    type_name = type(encrypted).__name__

    # CipherSeries:列名 → 密文(pandaseal 聚合后的常见形态)
    if type_name == "CipherSeries":
        rows = []
        for key in encrypted.index:
            cell = encrypted.loc[key]
            rows.append([str(key), _serialize_cipher(cell)])
        return [
            SheetData(
                name=primary_spec.name,
                headers=["column", "ciphertext"],
                rows=rows,
                chart=None,
            )
        ]

    # CipherDataFrame:二维,每 cell 是密文
    if type_name == "CipherDataFrame":
        try:
            col_names = list(encrypted.columns)
            idx_values = list(encrypted.index)
        except Exception:
            return [
                SheetData(
                    name=primary_spec.name,
                    headers=["info"],
                    rows=[["<encrypted_dataframe>"]],
                )
            ]
        headers = ["row_index"] + [str(c) for c in col_names]
        rows = []
        for idx in idx_values:
            row = [str(idx)]
            for col in col_names:
                try:
                    cell = encrypted.loc[idx, col]
                    row.append(_serialize_cipher(cell))
                except Exception:
                    row.append("<unreadable>")
            rows.append(row)
        return [SheetData(name=primary_spec.name, headers=headers, rows=rows)]

    # 单个 CipherArray
    if type_name == "CipherArray":
        return [
            SheetData(
                name=primary_spec.name,
                headers=["ciphertext"],
                rows=[[_serialize_cipher(encrypted)]],
            )
        ]

    # stub backend:bytes(JSON-wrapped 假密文)
    if isinstance(encrypted, bytes):
        text = encrypted.decode("utf-8", errors="replace")
        # 截断,避免巨长
        if len(text) > 1000:
            text = text[:1000] + " ...(truncated)"
        return [
            SheetData(
                name=primary_spec.name,
                headers=["ciphertext"],
                rows=[[text]],
            )
        ]

    # 兜底
    return [
        SheetData(
            name=primary_spec.name,
            headers=["ciphertext"],
            rows=[[_serialize_cipher(encrypted)]],
        )
    ]


def _write_excel_encrypted_node(state: AgentState) -> dict:
    """
    授权被拒时的输出节点:把**未解密的密文结果**写入 Excel。

    设计依据:用户拒绝授权 ≠ 任务失败。生成包含密文的 Excel 保留分析痕迹,
    持有正确密钥的用户后续可自行解出原始数值。
    """
    if state.get("error"):
        return {}
    plan = ComputationPlan.model_validate(state["computation_plan"])
    output: ExcelOutput = plan.output
    if not output:
        return {"error": "plan.output 缺失,无法写密文 Excel"}
    encrypted = state.get("encrypted_result")
    if encrypted is None:
        return {"error": "无密文结果可写"}

    sheets = _render_encrypted_to_sheets(encrypted, output.sheets, scenario=plan.scenario)
    # 文件名前缀加 _encrypted 与正常输出区分
    final_path = make_excel_path(prefix="analysis_encrypted")
    enforce_excel_path(final_path)  # B6 第 2 条仍生效
    try:
        result = write_excel(sheets, path=final_path)
    except Exception as e:
        return {"error": f"密文 Excel 写入失败: {e}"}

    note = (
        "\n\n注:解密授权被拒绝,Excel 中数据均为序列化后的密文,"
        "持有正确密钥的用户可在后续解密查看原始数值。"
    )
    summary = (state.get("summary_filtered") or "分析已完成。") + note
    return {
        "excel_path": str(result.path),
        "summary_filtered": summary,
    }


# ===========================================================================
# 构建图
# ===========================================================================


def build_workflow(
    *,
    llm_client: LLMClient,
    zfhe: ZFHE,
    pandaseal: PandaSeal,
    henumpy: HENumpy,
    helearn: HELearn,
    hetorch: HETorch,
    authorizer: DecryptionAuthorizer,
    max_retries: int = 2,
    system_prompt: Optional[str] = None,
):
    """构建并编译 LangGraph 工作流。"""
    deps = WorkflowDeps(
        llm=llm_client,
        zfhe=zfhe,
        pandaseal=pandaseal,
        henumpy=henumpy,
        helearn=helearn,
        hetorch=hetorch,
        authorizer=authorizer,
        max_retries=max_retries,
        system_prompt=system_prompt,
    )

    g = StateGraph(AgentState)

    # ----- 节点 -----
    g.add_node("prepare", _prepare_request)
    g.add_node("call_llm", _make_call_llm(deps))
    g.add_node("validate_plan", _validate_plan)
    g.add_node("filter_summary", _filter_summary)
    g.add_node("retry_llm", _retry_increment)
    g.add_node("fallback_summary", _fallback_summary_node)
    g.add_node("authorize", _make_authorize(deps))
    g.add_node("write_excel_encrypted", _write_excel_encrypted_node)
    g.add_node("compute_pandaseal", _make_compute(deps, "pandaseal"))
    g.add_node("compute_henumpy", _make_compute(deps, "henumpy"))
    g.add_node("compute_helearn", _make_compute(deps, "helearn"))
    g.add_node("compute_hetorch", _make_compute(deps, "hetorch"))
    g.add_node("compute_pipeline", _make_compute_pipeline(deps))
    g.add_node("decrypt", _make_decrypt(deps))
    g.add_node("write_excel", _write_excel_node)

    # ----- 边 -----
    g.add_edge(START, "prepare")
    g.add_edge("prepare", "call_llm")
    g.add_edge("call_llm", "validate_plan")
    g.add_conditional_edges(
        "validate_plan",
        _after_validate_plan,
        {"filter_summary": "filter_summary", "retry_or_fail": END},
    )
    g.add_conditional_edges(
        "filter_summary",
        lambda s: _after_filter_summary(s, max_retries),
        {
            # 场景路由出口
            "compute_pandaseal": "compute_pandaseal",
            "compute_henumpy": "compute_henumpy",
            "compute_helearn": "compute_helearn",
            "compute_hetorch": "compute_hetorch",
            "compute_pipeline": "compute_pipeline",
            "end_early": END,
            # 过滤命中的两条
            "retry_llm": "retry_llm",
            "fallback_summary": "fallback_summary",
        },
    )
    g.add_edge("retry_llm", "call_llm")

    # filter_summary 通过 → 直接按 scenario 路由到 compute(不再先走 authorize)
    # fallback_summary 走完也用同一组路由,保持兜底仍能产出 Excel
    scenario_routes = {
        "compute_pandaseal": "compute_pandaseal",
        "compute_henumpy": "compute_henumpy",
        "compute_helearn": "compute_helearn",
        "compute_hetorch": "compute_hetorch",
        "compute_pipeline": "compute_pipeline",
        "end_early": END,
    }
    # filter_summary 的 clean 分支重定向到场景路由
    # (filter_summary 已有 add_conditional_edges,改 "authorize" 出口为 "route_scenario")
    g.add_conditional_edges("fallback_summary", _route_scenario, scenario_routes)

    # compute_* → authorize(架构 §B6:解密前授权)→ decrypt
    for compute in (
        "compute_pandaseal",
        "compute_henumpy",
        "compute_helearn",
        "compute_hetorch",
        "compute_pipeline",
    ):
        g.add_edge(compute, "authorize")

    g.add_conditional_edges(
        "authorize",
        _after_authorize,
        {
            "decrypt": "decrypt",
            "write_excel_encrypted": "write_excel_encrypted",
            "end_early": END,
        },
    )
    g.add_edge("decrypt", "write_excel")
    g.add_edge("write_excel", END)
    g.add_edge("write_excel_encrypted", END)

    return g.compile()
