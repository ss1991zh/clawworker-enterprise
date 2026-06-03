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

def _resolve_to_numpy(full, x):
    """
    把 operand 取为 numpy ndarray(或标量 float):
      - 列名 → df[col].to_numpy()(显式 pandas → numpy)
      - 数字 → float 标量(np 广播)
      - 数字字符串 → float 标量
    返回 None 表示无法解析。
    """
    if isinstance(x, (int, float)):
        return float(x)
    if isinstance(x, str):
        if x in full.columns:
            return full[x].to_numpy()                # ← pandas → numpy
        try:
            return float(x)
        except ValueError:
            pass
    return None


def _apply_compute(full, spec: dict):
    """
    通用派生列计算 —— **显式 pandas → numpy → pandas 三段管线**:
      ① pandas:用 .to_numpy() 把所选列转成 ndarray
      ② numpy:用 np.add / np.subtract / np.multiply / np.divide 算结果
      ③ pandas:把结果 ndarray 赋回 df 的新列

    支持三种语法:
      ① 旧:{name, num, den}          → numpy.divide
      ② 通用:{name, op, operands: [...]} op ∈ add/sub/mul/div/ratio
         operands 元素可以是列名或常数
      ③ 表达式:{name, formula:"..."}  → 走 df.eval 兜底(列名含特殊字符需 backtick)
    """
    import numpy as np
    import pandas as pd

    name = spec.get("name")
    if not name:
        return
    op = (spec.get("op") or "").lower()

    # 形式①:num/den 比率(向后兼容)
    if not op and (spec.get("num") or spec.get("numerator")):
        num = spec.get("num") or spec.get("numerator")
        den = spec.get("den") or spec.get("denominator")
        if num in full.columns and den in full.columns:
            num_arr = full[num].to_numpy()
            den_arr = full[den].to_numpy()
            with np.errstate(divide="ignore", invalid="ignore"):
                full[name] = np.divide(num_arr, den_arr)
        return

    # 形式③:formula
    if spec.get("formula"):
        try:
            full[name] = full.eval(spec["formula"], engine="python")
        except Exception as e:
            raise ValueError(f"派生列「{name}」formula 解析失败: {e}")
        return

    # 形式②:op + operands(显式走 numpy)
    operands = spec.get("operands") or []
    if not operands:
        left = spec.get("left"); right = spec.get("right")
        if left is not None and right is not None:
            operands = [left, right]
    if not operands:
        return

    arrays = [_resolve_to_numpy(full, x) for x in operands]
    if any(v is None for v in arrays):
        raise ValueError(
            f"派生列「{name}」operands 含未知列/非数值: "
            f"{[x for x, v in zip(operands, arrays) if v is None]}"
        )

    if op in ("add", "sum"):
        result = arrays[0]
        for v in arrays[1:]:
            result = np.add(result, v)
    elif op == "sub":
        result = arrays[0]
        for v in arrays[1:]:
            result = np.subtract(result, v)
    elif op in ("mul", "prod"):
        result = arrays[0]
        for v in arrays[1:]:
            result = np.multiply(result, v)
    elif op in ("div", "ratio"):
        if len(arrays) < 2:
            raise ValueError(f"派生列「{name}」div 需要至少 2 个 operand")
        result = arrays[0]
        for v in arrays[1:]:
            with np.errstate(divide="ignore", invalid="ignore"):
                result = np.divide(result, v)
    else:
        raise ValueError(f"派生列「{name}」op「{op}」不支持(可用: add/sub/mul/div/ratio)")

    full[name] = result    # pandas ← numpy ndarray


def skill_row_detail(cdf, params, metadata_rows, metadata_columns):
    """
    逐行明细输出 — meta + 选定的数字列(可派生新列)。

    Params:
        value_cols: list[str](默认全部 cipher 列)
        compute: list[dict] 可选,要新增的派生列,支持三种语法:
                 ① 比率(旧): {name, num, den}
                 ② 通用 op:  {name, op:"add"/"sub"/"mul"/"div", operands:[列名或常数, ...]}
                 ③ 表达式:   {name, formula:"`实际销售额(元)` * 0.10"}
                 例:
                   {name:"销售提成", op:"mul",  operands:["实际销售额(元)", 0.10]}
                   {name:"应发提成合计", op:"add", operands:["销售提成", "绩效奖金"]}
                   {name:"完成率",   num:"实际", den:"目标"}
        sort_by: 排序列名(可选)
        ascending: 默认 False
        sheet_name: 默认 '逐行明细'
        n: 限制行数(可选)
    """
    import pandas as pd

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)

    # 派生列(按 compute 列表顺序执行 —— 后面的可以引用前面的派生列)
    for c in params.get("compute") or []:
        _apply_compute(full, c)

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


# ----- skill 7: forecast_linreg --------------------------------------------
#
# 三段架构(按 architecture 文档):
#   ① pandas      —— 读取密文 → 解密 → 清洗 / 时间归并 / 聚合
#   ② henumpy/ct  —— 把清洗后的数值数组重新加密成 HE numeric (ct.encrypt)
#   ③ helearn     —— hl.LinearRegression 在密态下 fit + predict
# 最后再用 ct.decrypt 把预测结果取回明文,拼成可读 DataFrame。

def _next_period_str(last_str: str, offset: int) -> str:
    """根据已有 time 字符串格式推下 N 期。
    支持 YYYY-MM(月) / YYYY-MM-DD(日) / YYYY(年);其他兜底拼 "+N"。
    """
    import re
    last_str = str(last_str).strip()
    if re.match(r"^\d{4}-\d{1,2}-\d{1,2}$", last_str):
        from datetime import date, timedelta
        try:
            y, m, d = (int(x) for x in last_str.split("-"))
            new = date(y, m, d) + timedelta(days=offset)
            return new.isoformat()
        except Exception:
            return f"{last_str}+{offset}"
    if re.match(r"^\d{4}-\d{1,2}$", last_str):
        try:
            y, m = (int(x) for x in last_str.split("-"))
            total = m + offset
            new_y = y + (total - 1) // 12
            new_m = (total - 1) % 12 + 1
            return f"{new_y:04d}-{new_m:02d}"
        except Exception:
            return f"{last_str}+{offset}"
    if re.match(r"^\d{4}$", last_str):
        return str(int(last_str) + offset)
    return f"{last_str}+{offset}"


def skill_forecast_linreg(cdf, params: dict, metadata_rows, metadata_columns):
    """
    时间序列预测 —— 按"数据分析助手"标准管线:
      ① pandas 清洗数据(解密 + 时间归并 + 聚合)
      ② henumpy / crypto_toolkit 把清洗后的数值数组再加密成 HE numeric
      ③ helearn.LinearRegression 在密态下 fit + predict

    params:
      value_col  : 要预测的数值列(必填,encrypted 列)
      time_col   : 时间维度列(必填,身份列里取)
      group_col  : 可选,按维度分别建模(每个 group 出一条预测线)
      n_periods  : 未来预测期数(默认 6)
      agg        : 历史值聚合 "sum" / "mean"(默认 "sum")
      iterations : helearn 迭代次数(默认 300)
      learning_rate : 学习率(默认 0.03)
      sheet_name : 输出 sheet 名

    输出列:[time_col][group_col?] 历史值 预测值 类型(历史/预测)
    """
    import pandas as pd
    import numpy as np

    value_col = params.get("value_col")
    time_col  = params.get("time_col")
    group_col = params.get("group_col") or None
    n_periods = int(params.get("n_periods") or 6)
    agg       = (params.get("agg") or "sum").lower()
    iterations    = int(params.get("iterations") or 300)
    learning_rate = float(params.get("learning_rate") or 0.03)
    sheet_name    = params.get("sheet_name") or f"{value_col or '数值'}_预测"

    # ── ① pandas:解密 + 拼明文身份列 + 清洗 ──────────────────────
    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)

    if not value_col or value_col not in full.columns:
        raise ValueError(
            f"forecast: value_col「{value_col}」无效 · "
            f"数值列可选: {[c for c in full.columns if pd.api.types.is_numeric_dtype(full[c])]}"
        )
    if not time_col or time_col not in full.columns:
        raise ValueError(
            f"forecast: time_col「{time_col}」无效 · 候选: {[c for c in full.columns if c != value_col]}"
        )

    cols_keep = [c for c in [time_col, group_col, value_col] if c]
    sub_all = full[cols_keep].copy()
    # 时间统一字符串化(允许 YYYY-MM / YYYY-MM-DD / pd.Timestamp)
    sub_all[time_col] = sub_all[time_col].astype(str).str.strip()
    sub_all = sub_all.dropna(subset=[time_col, value_col])

    # ── ② + ③:每个 group 跑一次 HE LinearRegression ─────────────
    from client.tools.runtime import Runtime
    Runtime.get().ensure_all_initialized()
    import crypto_toolkit as ct
    import helearn as hl

    groups = [None] if not group_col else sorted(sub_all[group_col].dropna().astype(str).unique().tolist())
    out_rows: list[dict] = []

    for g in groups:
        sub = sub_all if g is None else sub_all[sub_all[group_col].astype(str) == g]
        if sub.empty:
            continue

        # 按时间聚合(historic time-series)
        agg_fn = "mean" if agg == "mean" else "sum"
        ts = sub.groupby(time_col)[value_col].agg(agg_fn).sort_index()

        if len(ts) < 3:
            # 数据点太少 → 只保留历史不预测
            for t, v in ts.items():
                row = {time_col: str(t), "历史值": float(v), "预测值": None, "类型": "历史"}
                if g is not None: row[group_col] = g
                out_rows.append(row)
            continue

        # ── ② henumpy / ct:数据归一化 → 加密为 HE numeric ──────────
        # HE 梯度下降要求 X、y 尺度接近,否则 lr 会爆;
        # 归一化:X 除最大值到 [0,1],y 用 mean / std 标准化
        X_raw = np.arange(len(ts), dtype=np.float64).reshape(-1, 1)
        y_raw = ts.values.astype(np.float64)
        x_scale = max(float(X_raw.max()), 1.0)
        y_mean  = float(y_raw.mean())
        y_std   = float(y_raw.std()) if y_raw.std() > 1e-9 else 1.0

        X_norm = X_raw / x_scale                # [0, 1]
        y_norm = (y_raw - y_mean) / y_std        # 均值 0 方差 1

        try:
            X_cipher = ct.encrypt(X_norm)
            y_cipher = ct.encrypt(y_norm)
        except Exception as e:
            raise RuntimeError(f"forecast: 数值数组加密失败(ct.encrypt): {e}") from e

        # ── ③ helearn 训练(归一化数据 + 标准梯度下降) ───────────
        model = hl.LinearRegression(iterations=iterations, learningrate=learning_rate)
        model.fit(X_cipher, y_cipher)

        # 预测未来 n_periods(同样归一化)
        future_raw = np.arange(len(ts), len(ts) + n_periods, dtype=np.float64).reshape(-1, 1)
        future_norm = future_raw / x_scale
        try:
            future_X_cipher = ct.encrypt(future_norm)
            pred_cipher = model.predict(future_X_cipher)
            preds_norm = np.asarray(ct.decrypt(pred_cipher)).flatten()
        except Exception as e:
            raise RuntimeError(f"forecast: 预测/解密失败: {e}") from e

        # 反归一化
        preds_list = (preds_norm * y_std + y_mean).tolist()

        # 历史行
        for t, v in ts.items():
            row = {time_col: str(t), "历史值": float(v), "预测值": None, "类型": "历史"}
            if g is not None: row[group_col] = g
            out_rows.append(row)
        # 预测行
        last_time_str = str(ts.index[-1])
        for i, p in enumerate(preds_list[:n_periods], 1):
            row = {
                time_col: _next_period_str(last_time_str, i),
                "历史值": None,
                "预测值": float(p),
                "类型": "预测",
            }
            if g is not None: row[group_col] = g
            out_rows.append(row)

    out_df = pd.DataFrame(out_rows)
    col_order = [time_col] + ([group_col] if group_col else []) + ["历史值", "预测值", "类型"]
    out_df = out_df[[c for c in col_order if c in out_df.columns]]

    chart_hint = {
        "type": "line", "x": time_col, "y": ["历史值", "预测值"],
        "title": f"{value_col} · HE 线性回归预测",
    }
    return str(sheet_name)[:31], out_df.reset_index(drop=True), chart_hint


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
    "forecast_linreg": {
        "tool": "pandaseal+henumpy+helearn",
        "fn": skill_forecast_linreg,
        "desc": "时间序列预测 · pandas 清洗 → henumpy 加密数组 → helearn HE 线性回归 fit+predict",
        "params": ["value_col", "time_col", "group_col", "n_periods", "agg",
                   "iterations", "learning_rate", "sheet_name"],
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
