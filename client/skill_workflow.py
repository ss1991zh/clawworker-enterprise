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
    "completion_rate": "目标完成率",
    "achievement_rate": "目标完成率",
    "target_completion_rate": "目标完成率",
    "collection_rate": "回款率",
    "payback_rate": "回款率",
    "growth_rate": "增长率",
    "margin_rate": "边际贡献率",
    "contribution_rate": "边际贡献率",
    "commission_rate": "提成比例",
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

TIER_MAP_BY_COLNAME: dict[str, list[tuple[float, str]]] = {
    "目标完成率": COMPLETION_TIERS,
    "回款率": COLLECTION_TIERS,
}

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
}


def _looks_like_percent_column(name: str) -> bool:
    n = name.lower()
    return any(k.lower() in n for k in PERCENT_HINT_KEYWORDS)


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
    pct_fmt = "0.00%" if _looks_like_percent_column(result_col_name) else None

    # === 2. 列顺序调整:销售大区 紧贴在 销售代表 之前 ===
    cols_out = list(cols_in)
    if "销售大区" in cols_out and "销售代表" in cols_out:
        cols_out.remove("销售大区")
        idx = cols_out.index("销售代表")
        cols_out.insert(idx, "销售大区")

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
    return [s for s in sheets if s is not None]


def _fmt_pct(v: float) -> str:
    """0.1234 → '12.34%'。"""
    return f"{v * 100:.2f}%"


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
    hit = sum(1 for v in vals if v >= rule["threshold"])
    sorted_recs = sorted(records, key=lambda r: r[result_col_name], reverse=True)
    top3 = sorted_recs[:3]
    bottom3 = sorted_recs[-3:][::-1]  # 倒序展示,最差排第一

    def _name(rec: dict) -> str:
        return rec.get("销售代表") or rec.get("员工编号") or "?"

    return [
        KpiCard(
            label=rule["average_label"],
            value=_fmt_pct(mean_v),
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
            value="\n".join(f"{_name(r)} {_fmt_pct(r[result_col_name])}" for r in top3),
            subtitle="按完成率降序",
            bg_color="E3F2FD",
            value_size=12,
        ),
        KpiCard(
            label=rule["bottom_label"],
            value="\n".join(f"{_name(r)} {_fmt_pct(r[result_col_name])}" for r in bottom3),
            subtitle="按完成率升序",
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
    """明细:KPI + 100 行表(无大图,档位涂色已经足够直观)。"""
    headers = cols_out + [result_col_name]
    # 按 大区 + 代表 排序
    sorted_recs = sorted(
        records,
        key=lambda r: (
            str(r.get("销售大区") or ""),
            str(r.get("销售代表") or ""),
        ),
    )
    rows = [[r.get(c) for c in cols_out] + [r[result_col_name]] for r in sorted_recs]

    number_formats: dict[str, str] = {}
    if pct_fmt:
        number_formats[result_col_name] = pct_fmt

    tier_colors: dict[str, list[tuple[float, str]]] = {}
    if result_col_name in TIER_MAP_BY_COLNAME:
        tier_colors[result_col_name] = TIER_MAP_BY_COLNAME[result_col_name]

    return SheetData(
        name=f"{result_col_name}明细",
        headers=headers,
        rows=rows,
        chart=None,  # 明细页不放大图(用 KPI + 档位涂色替代)
        number_formats=number_formats,
        tier_colors=tier_colors,
        kpi_cards=_build_kpi_cards(sorted_recs, result_col_name),
    )


def _build_region_summary_sheet(
    *, records: list[dict], result_col_name: str, pct_fmt: Optional[str]
) -> Optional[SheetData]:
    """大区汇总 sheet。"""
    if not records or "销售大区" not in records[0]:
        return None
    regions: dict[str, list[float]] = {}
    for r in records:
        regions.setdefault(r["销售大区"], []).append(r[result_col_name])

    summary = []
    for region, vals in regions.items():
        summary.append(
            {
                "销售大区": region,
                "订单数": len(vals),
                f"平均{result_col_name}": sum(vals) / len(vals),
                f"最高{result_col_name}": max(vals),
                f"最低{result_col_name}": min(vals),
            }
        )
    # 按平均率降序
    summary.sort(key=lambda r: r[f"平均{result_col_name}"], reverse=True)

    headers = ["销售大区", "订单数", f"平均{result_col_name}", f"最高{result_col_name}", f"最低{result_col_name}"]
    rows = [[r[h] for h in headers] for r in summary]

    number_formats: dict[str, str] = {}
    if pct_fmt:
        for h in headers:
            if "率" in h or "完成" in h or "回款" in h:
                number_formats[h] = pct_fmt

    chart = ChartSpec(
        type="bar",
        x="销售大区",
        y=f"平均{result_col_name}",
        title=f"各大区平均{result_col_name}",
    )
    region_colors = [REGION_COLORS.get(r["销售大区"], DEFAULT_REGION_COLOR) for r in summary]

    return SheetData(
        name=f"大区汇总-{result_col_name}",
        headers=headers,
        rows=rows,
        chart=chart,
        number_formats=number_formats,
        series_colors_by_row=region_colors,
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
    base_cols = [c for c in ("员工编号", "销售代表", "销售大区", "城市", "产品线", "销售月份") if c in picked[0]]
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

    chart = None
    if "销售代表" in base_cols:
        chart = ChartSpec(type="bar", x="销售代表", y=result_col_name, title=sheet_name)
    region_colors = (
        [REGION_COLORS.get(r.get("销售大区"), DEFAULT_REGION_COLOR) for r in picked]
        if "销售大区" in base_cols
        else None
    )

    return SheetData(
        name=sheet_name[:31],
        headers=headers,
        rows=rows,
        chart=chart,
        number_formats=number_formats,
        tier_colors=tier_colors,
        series_colors_by_row=region_colors,
    )


def _render_to_sheets(
    decrypted: Any,
    sheet_specs: list[SheetSpec],
    scenario: Scenario,
    metadata_rows: Optional[list] = None,
    metadata_columns: Optional[list[str]] = None,
) -> list[SheetData]:
    """
    把解密后的结果按 sheet spec 渲染成可写入 Excel 的 SheetData。

    metadata_rows: 若提供且与 decrypted 行数对齐,自动合并到输出
                    (用于"加密数字 + 明文标识"双通道场景,如逐人完成率)
    """
    if not sheet_specs:
        sheet_specs = [SheetSpec(name="Result")]

    primary_spec = sheet_specs[0]

    # 情况 -1:row-aligned list + metadata → 合并为丰富表(每人一行)
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
