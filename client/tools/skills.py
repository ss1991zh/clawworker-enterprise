"""
Skill 模板库 — LLM 的新执行接口。

设计思路(对照 zionskill SKILL.md):
- LLM 不再写 op 序列,改成"挑 skill + 填字段名"
- 每个 skill 是一个固化的业务模板(对的 ps/hp/hl 调用顺序 + metadata 合并)
- 每个 SkillCall 产出一份 sheet(sheet_name, DataFrame)
- 多 SkillCall 自动出多 sheet Excel

每个 skill 函数签名:
    fn(cdf, params, metadata_rows, metadata_columns) -> (sheet_name, df_for_excel, chart_hint)

调用方(skill_workflow)负责:
  - 加载 cipher → CipherDataFrame
  - 鉴权(B6-1)
  - 调度 skill_calls
  - 合并产出的 (sheet_name, df) 列表 → renderer
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 各 skill 实现
# ---------------------------------------------------------------------------


def _merge_meta(decrypted_df, metadata_rows, metadata_columns):
    """通用:把解密后的数字列横拼上 metadata 身份列。"""
    import pandas as pd
    if metadata_rows and len(metadata_rows) == len(decrypted_df):
        meta_df = pd.DataFrame(metadata_rows)
        if metadata_columns:
            keep = [c for c in metadata_columns if c in meta_df.columns]
            if keep:
                meta_df = meta_df[keep]
        meta_keep = [c for c in meta_df.columns if c not in decrypted_df.columns]
        return pd.concat(
            [meta_df[meta_keep].reset_index(drop=True),
             decrypted_df.reset_index(drop=True)],
            axis=1,
        )
    return decrypted_df.reset_index(drop=True)


def _decrypt(cdf):
    """统一解密:CipherDataFrame → pandas DataFrame。"""
    import crypto_toolkit as ct
    return ct.decrypt_df(cdf)


# ----- skill 1: ratio_by_group --------------------------------------------

def skill_ratio_by_group(cdf, params: dict, metadata_rows, metadata_columns):
    """
    按 group_col 分组,对每组算 sum(num) / sum(den) 比率。
    适用场景:回款率(回款/销售额)、目标完成率(实际/目标)、毛利率等。

    Params:
        num_col: 分子列名(必须是 cipher 数字列)
        den_col: 分母列名(必须是 cipher 数字列)
        group_col: 分组列名(必须是 metadata 字符串列)
        metric_name: 输出比率列叫什么(默认 'ratio')
        sheet_name: 输出 sheet 名(默认 '按{group}-{metric}')
        ascending: 排序方向,默认 False(降序)
    """
    import pandas as pd

    num = params["num_col"]
    den = params["den_col"]
    group = params["group_col"]
    metric = params.get("metric_name") or "ratio"

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)

    if group not in full.columns:
        raise ValueError(f"ratio_by_group: group_col 「{group}」不存在 · 可选: {list(full.columns)[:20]}")
    if num not in full.columns or den not in full.columns:
        raise ValueError(f"ratio_by_group: num/den 列缺失 · num={num} den={den}")

    grouped = full.groupby(group, as_index=False).agg(
        订单数=(num, 'count'),
        分子总和=(num, 'sum'),
        分母总和=(den, 'sum'),
        最大值=(num, 'max'),
        最小值=(num, 'min'),
    )
    grouped[metric] = grouped['分子总和'] / grouped['分母总和']
    out = grouped[[group, '订单数', metric, '最大值', '最小值']]
    out = out.sort_values(metric, ascending=bool(params.get('ascending', False)))

    sheet_name = params.get("sheet_name") or f"按{group}-{metric}"
    chart = {
        "type": "bar",
        "x": group,
        "y": metric,
        "title": sheet_name,
    }
    return sheet_name, out.reset_index(drop=True), chart


# ----- skill 2: row_ratio_then_group_mean ---------------------------------

def skill_row_ratio_then_group_mean(cdf, params, metadata_rows, metadata_columns):
    """
    先算每行的 num/den 行级率,再按 group_col 取均值。
    与 ratio_by_group 的差别:这个等权,ratio_by_group 是按基数加权。

    Params 同 ratio_by_group。
    """
    import pandas as pd

    num = params["num_col"]
    den = params["den_col"]
    group = params["group_col"]
    metric = params.get("metric_name") or "avg_ratio"

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)

    if num not in full.columns or den not in full.columns:
        raise ValueError(f"row_ratio_then_group_mean: 列缺失 num={num} den={den}")
    if group not in full.columns:
        raise ValueError(f"row_ratio_then_group_mean: group_col 「{group}」不存在")

    full["__ratio__"] = full[num] / full[den]
    grouped = full.groupby(group, as_index=False).agg(
        订单数=(num, 'count'),
        平均比率=("__ratio__", 'mean'),
        最高=("__ratio__", 'max'),
        最低=("__ratio__", 'min'),
    ).rename(columns={'平均比率': metric})
    grouped = grouped.sort_values(metric, ascending=bool(params.get("ascending", False)))

    sheet_name = params.get("sheet_name") or f"按{group}-{metric}(行级均)"
    chart = {"type": "bar", "x": group, "y": metric, "title": sheet_name}
    return sheet_name, grouped.reset_index(drop=True), chart


# ----- skill 3: top_n_by --------------------------------------------------

def skill_top_n_by(cdf, params, metadata_rows, metadata_columns):
    """
    按 value_col 取 TOP/BOTTOM N 行,带完整身份列(员工编号/姓名/大区...)。

    Params:
        value_col: 排序的数字列(必填)
        n: 取几个(默认 10)
        ascending: True=BOTTOM N,False=TOP N(默认 False)
        sheet_name: 默认 'TOP/BOTTOM {n} {value_col}'
    """
    import pandas as pd

    value_col = params["value_col"]
    n = int(params.get("n", 10))
    ascending = bool(params.get("ascending", False))

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)

    if value_col not in full.columns:
        raise ValueError(f"top_n_by: value_col「{value_col}」不存在")

    sorted_df = full.sort_values(value_col, ascending=ascending).head(n).reset_index(drop=True)
    sorted_df.insert(0, "排名", range(1, len(sorted_df) + 1))

    rank_type = "BOTTOM" if ascending else "TOP"
    sheet_name = params.get("sheet_name") or f"{rank_type}{n} {value_col}"
    chart = {"type": "bar", "x": "排名", "y": value_col, "title": sheet_name}
    return sheet_name, sorted_df, chart


# ----- skill 4: group_stats -----------------------------------------------

def skill_group_stats(cdf, params, metadata_rows, metadata_columns):
    """
    按 group_col 分组,对每个 value_col 算多个聚合(mean/max/min/count/sum/std)。

    Params:
        group_col: 分组维度(meta 列)
        value_cols: list[str] 要算的数字列
        aggs: list[str] 聚合方式(默认 ['mean','max','min','count'])
        sheet_name: 默认 '按{group}统计'
    """
    import pandas as pd

    group = params["group_col"]
    value_cols = list(params["value_cols"])
    aggs = list(params.get("aggs") or ["mean", "max", "min", "count"])

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)

    if group not in full.columns:
        raise ValueError(f"group_stats: group_col 「{group}」不存在")
    miss = [c for c in value_cols if c not in full.columns]
    if miss:
        raise ValueError(f"group_stats: value_cols 列不存在: {miss}")

    grouped = full.groupby(group).agg({c: aggs for c in value_cols})
    grouped.columns = [f"{c}_{a}" for c, a in grouped.columns]
    grouped = grouped.reset_index()

    sheet_name = params.get("sheet_name") or f"按{group}统计"
    chart_y = grouped.columns[1] if len(grouped.columns) > 1 else None
    chart = (
        {"type": "bar", "x": group, "y": chart_y, "title": sheet_name}
        if chart_y else None
    )
    return sheet_name, grouped, chart


# ----- skill 5: describe --------------------------------------------------

def skill_describe(cdf, params, metadata_rows, metadata_columns):
    """
    整体描述统计 count / mean / std / min / max。

    Params:
        value_cols: list[str](默认全部 cipher 列)
        sheet_name: 默认 '描述统计'
    """
    import pandas as pd

    decrypted = _decrypt(cdf)
    cols = params.get("value_cols")
    if cols:
        cols = [c for c in cols if c in decrypted.columns]
        if cols:
            decrypted = decrypted[cols]
    desc = decrypted.describe().T.reset_index().rename(columns={"index": "字段"})
    sheet_name = params.get("sheet_name") or "描述统计"
    return sheet_name, desc, None


# ----- skill 6: row_detail ------------------------------------------------

def skill_row_detail(cdf, params, metadata_rows, metadata_columns):
    """
    逐行明细输出 — meta + 选定的数字列。
    适合"展示每位员工目标完成率/回款率明细"这类需求。

    Params:
        value_cols: list[str](默认全部 cipher 列)
        compute: list[dict] 可选,要新增的派生列,每个 {name, num, den}
                 例:[{"name":"目标完成率","num":"实际销售额","den":"月度销售目标"}]
        sort_by: 排序列名(可选)
        ascending: 默认 False
        sheet_name: 默认 '逐行明细'
        n: 限制行数(可选)
    """
    import pandas as pd

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)

    # 派生列
    for c in params.get("compute") or []:
        name = c.get("name")
        num = c.get("num") or c.get("numerator")
        den = c.get("den") or c.get("denominator")
        if name and num in full.columns and den in full.columns:
            full[name] = full[num] / full[den]

    # 选列
    cols = params.get("value_cols")
    if cols:
        keep = [c for c in cols if c in full.columns]
        meta_cols = [c for c in (metadata_columns or []) if c in full.columns]
        full = full[meta_cols + [c for c in keep if c not in meta_cols]]

    # 排序
    sort_by = params.get("sort_by")
    if sort_by and sort_by in full.columns:
        full = full.sort_values(sort_by, ascending=bool(params.get("ascending", False)))

    # 限制行数
    n = params.get("n")
    if n:
        full = full.head(int(n))

    sheet_name = params.get("sheet_name") or "逐行明细"
    return sheet_name, full.reset_index(drop=True), None


# ---------------------------------------------------------------------------
# Skill 注册表 — LLM 必须从这里选
# ---------------------------------------------------------------------------

SKILLS: dict[str, dict[str, Any]] = {
    "ratio_by_group": {
        "tool": "pandaseal",
        "fn": skill_ratio_by_group,
        "desc": "按维度分组算每组 sum(num)/sum(den) 比率(基数加权 · 回款率/完成率/库存周转)",
        "params": ["num_col", "den_col", "group_col", "metric_name", "ascending", "sheet_name"],
    },
    "row_ratio_then_group_mean": {
        "tool": "pandaseal",
        "fn": skill_row_ratio_then_group_mean,
        "desc": "先算每行 num/den 行级率,再按维度取均值(等权平均率)",
        "params": ["num_col", "den_col", "group_col", "metric_name", "ascending", "sheet_name"],
    },
    "top_n_by": {
        "tool": "pandaseal",
        "fn": skill_top_n_by,
        "desc": "按值取 TOP / BOTTOM N(ascending=true 为 BOTTOM)· 带身份列",
        "params": ["value_col", "n", "ascending", "sheet_name"],
    },
    "group_stats": {
        "tool": "pandaseal",
        "fn": skill_group_stats,
        "desc": "按维度分组,对多个数字列算多个聚合(mean/max/min/count/sum/std)",
        "params": ["group_col", "value_cols", "aggs", "sheet_name"],
    },
    "describe": {
        "tool": "pandaseal",
        "fn": skill_describe,
        "desc": "整体描述统计 count/mean/std/min/max",
        "params": ["value_cols", "sheet_name"],
    },
    "row_detail": {
        "tool": "pandaseal",
        "fn": skill_row_detail,
        "desc": "逐行明细 + 可选派生比率列 · 适合「展示每位员工目标完成率」",
        "params": ["value_cols", "compute", "sort_by", "ascending", "n", "sheet_name"],
    },
}


def skill_names() -> list[str]:
    return list(SKILLS.keys())


def get_skill(name: str) -> Optional[dict]:
    return SKILLS.get(name)


def run_skill(name: str, cdf, params, metadata_rows, metadata_columns):
    """便捷调用入口。"""
    s = SKILLS.get(name)
    if not s:
        raise ValueError(f"未知 skill 「{name}」 · 可用: {list(SKILLS.keys())}")
    return s["fn"](cdf, params, metadata_rows, metadata_columns)
