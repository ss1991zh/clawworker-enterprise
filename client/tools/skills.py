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
  - 鉴权(解密授权 / HITL)
  - 调度 skill_calls
  - 合并产出的 (sheet_name, df) 列表 → renderer
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# 各 skill 实现
# ---------------------------------------------------------------------------


_SOURCE_TOTAL_KEYS = ("合计", "总计", "汇总", "累计", "总和", "total")


def drop_source_total_rows(df):
    """
    剔除**源数据自带**的合计行,返回 (df, 剔除行数)。

    业务 Excel 常在表尾带一行合计(身份列几乎全空、只有数值汇总,或写着"合计")。
    它不是一条真实记录,混进分析会把人数 +1、总和翻倍、比率污染,
    输出时系统/LLM 再加合计就成了双重合计。
    只扫**表尾连续最多 3 行**,避免误删中间正常数据;判定条件(满足其一):
      · 任一身份列值含 合计/总计/汇总/Total 字样
      · 身份列非空数 ≤ 1(几乎全空)而数值列有值(且身份列 ≥ 2 个才启用此规则)
    """
    import pandas as pd
    if df is None or len(df) == 0:
        return df, 0
    num_cols = df.select_dtypes(include="number").columns
    id_cols = [c for c in df.columns if c not in num_cols]
    if not id_cols:
        return df, 0

    def _is_total(row) -> bool:
        vals = []
        for c in id_cols:
            v = row[c]
            vals.append("" if pd.isna(v) else str(v).strip())
        if any(any(k in v.lower() for k in _SOURCE_TOTAL_KEYS) for v in vals if v):
            return True
        nonempty = sum(1 for v in vals if v and v.lower() not in ("nan", "none"))
        has_num = bool(len(num_cols)) and any(pd.notna(row[c]) for c in num_cols)
        return len(id_cols) >= 2 and nonempty <= 1 and has_num

    n = len(df)
    drop_pos = []
    for i in range(n - 1, max(-1, n - 4), -1):   # 表尾最多 3 行
        if _is_total(df.iloc[i]):
            drop_pos.append(i)
        else:
            break
    if not drop_pos:
        return df, 0
    return df.drop(df.index[drop_pos]).reset_index(drop=True), len(drop_pos)


def _merge_meta(decrypted_df, metadata_rows, metadata_columns):
    """通用:把解密后的数字列横拼上 metadata 身份列(并剔除源数据自带的合计行)。"""
    import pandas as pd
    if metadata_rows and len(metadata_rows) == len(decrypted_df):
        meta_df = pd.DataFrame(metadata_rows)
        if metadata_columns:
            keep = [c for c in metadata_columns if c in meta_df.columns]
            if keep:
                meta_df = meta_df[keep]
        meta_keep = [c for c in meta_df.columns if c not in decrypted_df.columns]
        merged = pd.concat(
            [meta_df[meta_keep].reset_index(drop=True),
             decrypted_df.reset_index(drop=True)],
            axis=1,
        )
        merged, _ = drop_source_total_rows(merged)
        return merged
    return decrypted_df.reset_index(drop=True)


def _decrypt(cdf):
    """统一解密:CipherDataFrame → pandas DataFrame。"""
    import crypto_toolkit as ct
    return ct.decrypt_df(cdf)


def _inf_to_nan(values):
    """
    除法结果清理:±inf(除零产物)→ NaN。
    不清理的话 inf 会在降序排序里霸榜第一名;NaN 排序时自动沉底(na_position 默认 last)。
    """
    import numpy as np
    arr = np.asarray(values, dtype=float)
    return np.where(np.isfinite(arr), arr, np.nan)


# ----- 时间优先排序(有时间列时,指标高低降一级,时间序在前)-----

# 时间列名关键词:含这些词(或 datetime dtype)即视为时间维度。
# 刻意不用裸「月/年/周」做子串(会误伤「月饼」「年龄」),改用完整词 + endswith("月")。
_TIME_COL_KEYS = ("月份", "年月", "年份", "日期", "时间", "季度", "月度", "周次",
                  "date", "month", "year", "quarter", "week", "period", "time")


def _is_time_col(name, series=None) -> bool:
    """该列是否是时间维度:datetime dtype,或列名含时间关键词。"""
    import pandas as pd
    if series is not None and pd.api.types.is_datetime64_any_dtype(series):
        return True
    s = str(name).strip().lower()
    return any(k in s for k in _TIME_COL_KEYS) or s.endswith("月")


def _time_sort_series(series):
    """
    把时间列转成可排序键,解决「"10月" < "2月"」的字符串排序陷阱:
    datetime/数值 → 原样;字符串先试 to_datetime,再退化为提取数字序列
    ("2026年3月" → (2026,3)、"Q2" → (2,));完全无数字排最后。
    """
    import re
    import pandas as pd
    s = series
    if pd.api.types.is_datetime64_any_dtype(s) or pd.api.types.is_numeric_dtype(s):
        return s
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")   # 混合格式时 to_datetime 会告警,静默逐元素解析即可
        dt = pd.to_datetime(s, errors="coerce")
    if dt.notna().mean() >= 0.8:
        return dt

    def _key(v):
        txt = re.sub(r"[〇零一二两三四五六七八九十]+",
                     lambda m: (lambda n: str(n) if n is not None else m.group(0))(
                         _cn_to_num(m.group(0))),
                     str(v))
        nums = re.findall(r"\d+", txt)
        return tuple(int(x) for x in nums) if nums else (float("inf"),)

    return s.map(_key)


def _cn_to_num(t: str):
    """中文数字 → 阿拉伯数字:「三」→3、「十月」的「十」→10、「二十三」→23、
    「二〇二六」→2026(逐字)。解析不了返回 None。覆盖月/季/周/年场景(<100 + 逐字年份)。"""
    digits = {"〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if not t:
        return None
    if "十" in t:
        a, _, b = t.partition("十")
        if (a and a not in digits) or (b and b not in digits):
            return None
        return (digits[a] if a else 1) * 10 + (digits[b] if b else 0)
    if all(ch in digits for ch in t):
        return int("".join(str(digits[ch]) for ch in t))
    return None


def _sort_group_result(out, group, metric, ascending):
    """分组汇总表排序:分组维度本身是时间(按月/按季…)→ 时间升序;否则按指标排序。"""
    if _is_time_col(group, out[group]):
        key = "__tkey__"
        out = out.assign(**{key: _time_sort_series(out[group])})
        return out.sort_values(key).drop(columns=[key])
    return out.sort_values(metric, ascending=ascending)


def _smart_sort(full, sort_by, ascending, metadata_columns):
    """
    逐行明细表的时间优先排序:
      无时间列 → 按 sort_by 排序(原行为);
      有时间列 → 行序 = 实体(产品/大区…) → 组内时间升序;
                 sort_by 指标降一级,只决定**实体之间**的先后(按实体均值排)。
    """
    import pandas as pd
    time_cols = [c for c in full.columns if _is_time_col(c, full[c])]
    if not time_cols:
        return full.sort_values(sort_by, ascending=ascending)
    tcol = time_cols[0]
    tkey = "__tkey__"
    full = full.assign(**{tkey: _time_sort_series(full[tcol])})
    entity_cols = [c for c in (metadata_columns or [])
                   if c in full.columns and not _is_time_col(c, full[c])]
    if entity_cols and pd.api.types.is_numeric_dtype(full[sort_by]):
        ecol = entity_cols[0]
        rank = full.groupby(ecol)[sort_by].mean().sort_values(ascending=ascending)
        order = {v: i for i, v in enumerate(rank.index)}
        ekey = "__ekey__"
        full = full.assign(**{ekey: full[ecol].map(order)})
        return full.sort_values([ekey, tkey]).drop(columns=[ekey, tkey])
    return full.sort_values(tkey).drop(columns=[tkey])


def _apply_filter(full, params):
    """
    按 params['filter'] 过滤行,让"只看某个产品 / 大区 / 客户 / 员工"生效。
    filter = {列名: 值} 或 {列名: [值, ...]};多列之间为 AND。

    匹配策略:先精确匹配;精确无命中则退化为**不区分大小写的子串包含**
    (应对用户只说 "DR-400" 而单元格是 "数控伺服驱动器 DR-400" 的情况)。
    · 列不存在 → 跳过该条件(不致误删);
    · 某条件最终零匹配 → 报错并列出样例值,**避免"点名筛选却返回全部数据"或静默空表**。
    """
    import re as _re
    flt = params.get("filter")
    if not flt or not isinstance(flt, dict):
        return full
    out = full
    for col, val in flt.items():
        if col not in out.columns:
            continue
        s = out[col].astype(str).str.strip()
        if isinstance(val, (list, tuple, set)):
            wanted = [str(v).strip() for v in val if str(v).strip()]
            if not wanted:
                continue
            mask = s.isin(wanted)
            if not mask.any():
                mask = s.apply(lambda x: any(w.lower() in x.lower() for w in wanted))
        else:
            v = str(val).strip()
            if not v:
                continue
            mask = s == v
            if not mask.any():
                mask = s.str.contains(_re.escape(v), case=False, na=False)
        if not mask.any():
            sample = sorted(set(s.dropna().tolist()))[:8]
            raise ValueError(
                f"filter: 列「{col}」中未找到匹配「{val}」的行 · 该列样例值: {sample}"
            )
        out = out[mask]
    return out.reset_index(drop=True)


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
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)

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
    grouped[metric] = _inf_to_nan(grouped['分子总和'] / grouped['分母总和'])  # 除零沉底不霸榜
    out = grouped[[group, '订单数', metric, '最大值', '最小值']]
    # 分组维度是时间(按月/按季…)→ 时间升序,指标高低降一级;否则按指标降序
    out = _sort_group_result(out, group, metric, bool(params.get('ascending', False)))

    sheet_name = params.get("sheet_name") or f"按{group}-{metric}"
    chart = {
        "type": "line" if _is_time_col(group, out[group]) else "bar",
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
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)

    if num not in full.columns or den not in full.columns:
        raise ValueError(f"row_ratio_then_group_mean: 列缺失 num={num} den={den}")
    if group not in full.columns:
        raise ValueError(f"row_ratio_then_group_mean: group_col 「{group}」不存在")

    full["__ratio__"] = _inf_to_nan(full[num] / full[den])  # 除零沉底不霸榜
    grouped = full.groupby(group, as_index=False).agg(
        订单数=(num, 'count'),
        平均比率=("__ratio__", 'mean'),
        最高=("__ratio__", 'max'),
        最低=("__ratio__", 'min'),
    ).rename(columns={'平均比率': metric})
    # 分组维度是时间 → 时间升序;否则按指标降序
    grouped = _sort_group_result(grouped, group, metric, bool(params.get("ascending", False)))

    sheet_name = params.get("sheet_name") or f"按{group}-{metric}(行级均)"
    chart = {"type": "line" if _is_time_col(group, grouped[group]) else "bar",
             "x": group, "y": metric, "title": sheet_name}
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
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)

    if value_col not in full.columns:
        raise ValueError(f"top_n_by: value_col「{value_col}」不存在")

    sorted_df = full.sort_values(value_col, ascending=ascending).head(n).reset_index(drop=True)
    # 并列同名次(1,1,3 式):同值共享名次,而不是按行号硬编 1,2,3
    ranks = sorted_df[value_col].rank(method="min", ascending=ascending)
    sorted_df.insert(0, "排名", ranks.fillna(len(sorted_df)).astype(int))

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
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)

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
    # 支持按指定实体筛选(filter 作用在身份列上,再对数值列做描述统计)
    if params.get("filter"):
        full = _apply_filter(_merge_meta(decrypted, metadata_rows, metadata_columns), params)
        decrypted = full.select_dtypes(include="number")
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
                full[name] = _inf_to_nan(np.divide(num_arr, den_arr))
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
        result = _inf_to_nan(result)
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
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)

    # 派生列(按 compute 列表顺序执行 —— 后面的可以引用前面的派生列)
    for c in params.get("compute") or []:
        _apply_compute(full, c)

    # 选列
    cols = params.get("value_cols")
    if cols:
        keep = [c for c in cols if c in full.columns]
        meta_cols = [c for c in (metadata_columns or []) if c in full.columns]
        full = full[meta_cols + [c for c in keep if c not in meta_cols]]

    # 排序(时间优先:含时间列时 实体→时间升序,sort_by 指标只决定实体间先后)
    sort_by = params.get("sort_by")
    if sort_by and sort_by in full.columns:
        full = _smart_sort(full, sort_by, bool(params.get("ascending", False)),
                           metadata_columns)

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
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)

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
    # 多维度(多产品/多大区)→ 按维度拆图,每组各一张趋势图(避免挤一张没法看)
    if group_col and group_col in out_df.columns and out_df[group_col].nunique() > 1:
        chart_hint["split_by"] = group_col
    return str(sheet_name)[:31], out_df.reset_index(drop=True), chart_hint


# ===========================================================================
# 业务分析固化 skill(第一梯队 · 几乎通用)
# ===========================================================================

# ----- skill: 同比/环比分析 -------------------------------------------------

def skill_yoy_mom(cdf, params, metadata_rows, metadata_columns):
    """
    同比(YoY)/ 环比(MoM)增长分析。
    params:
      time_col   : 时间列(YYYY-MM 月 / YYYY 年,meta 列)
      value_col  : 数值列(encrypted)
      group_col? : 可选维度,分组各自算
      mode?      : "mom"(环比·上一期)/ "yoy"(同比·去年同期,月数据=12期前)/ "both"(默认)
      agg?       : 同一时间多行时的聚合 "sum"(默认)/ "mean"
    输出:time [group] 值 环比 环比率 同比 同比率
    """
    import pandas as pd
    import numpy as np

    time_col = params["time_col"]
    value_col = params["value_col"]
    group_col = params.get("group_col") or None
    mode = (params.get("mode") or "both").lower()
    agg = "mean" if (params.get("agg") or "sum").lower() == "mean" else "sum"

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)
    for c in (time_col, value_col):
        if c not in full.columns:
            raise ValueError(f"yoy_mom: 列「{c}」不存在")

    keys = [group_col, time_col] if group_col else [time_col]
    ts = full.groupby(keys)[value_col].agg(agg).reset_index()

    out_frames = []
    groups = [None] if not group_col else sorted(ts[group_col].astype(str).unique())
    for g in groups:
        sub = ts if g is None else ts[ts[group_col].astype(str) == g]
        sub = sub.sort_values(time_col).reset_index(drop=True)
        sub = sub.rename(columns={value_col: "本期值"})
        # 环比:上一期
        sub["环比增长"] = sub["本期值"].diff()
        with np.errstate(divide="ignore", invalid="ignore"):
            sub["环比率"] = sub["环比增长"] / sub["本期值"].shift(1)
        # 同比:12 期前(月)/ 1 期前兜底
        lag = 12 if len(sub) > 12 else max(1, len(sub) - 1)
        sub["同比增长"] = sub["本期值"] - sub["本期值"].shift(lag)
        with np.errstate(divide="ignore", invalid="ignore"):
            sub["同比率"] = sub["同比增长"] / sub["本期值"].shift(lag)
        out_frames.append(sub)

    res = pd.concat(out_frames, ignore_index=True)
    # 按 mode 裁列
    cols = ([group_col] if group_col else []) + [time_col, "本期值"]
    if mode in ("mom", "both"):
        cols += ["环比增长", "环比率"]
    if mode in ("yoy", "both"):
        cols += ["同比增长", "同比率"]
    res = res[[c for c in cols if c in res.columns]]

    sheet = params.get("sheet_name") or f"{value_col}_同比环比"
    chart = {"type": "line", "x": time_col, "y": "本期值", "title": sheet}
    return str(sheet)[:31], res, chart


# ----- skill: 预算差异分析 --------------------------------------------------

def skill_budget_variance(cdf, params, metadata_rows, metadata_columns):
    """
    预算 vs 实际 差异分析。
    params:
      budget_col, actual_col : 预算列 / 实际列(encrypted)
      group_col?             : 可选分组维度(不给=逐行)
      favorable?             : "higher"(实际越高越好,如收入·默认)/ "lower"(越低越好,如成本)
    输出:[group] 预算 实际 差异 差异率 评价(超支/达标/节约)
    """
    import pandas as pd
    import numpy as np

    budget_col = params["budget_col"]
    actual_col = params["actual_col"]
    group_col = params.get("group_col") or None
    favorable = (params.get("favorable") or "higher").lower()

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)
    for c in (budget_col, actual_col):
        if c not in full.columns:
            raise ValueError(f"budget_variance: 列「{c}」不存在")

    if group_col and group_col in full.columns:
        g = full.groupby(group_col)[[budget_col, actual_col]].sum().reset_index()
    else:
        keep = [c for c in (metadata_columns or []) if c in full.columns]
        g = full[keep + [budget_col, actual_col]].copy()

    g["差异"] = g[actual_col].to_numpy() - g[budget_col].to_numpy()
    with np.errstate(divide="ignore", invalid="ignore"):
        g["差异率"] = np.divide(g["差异"].to_numpy(), g[budget_col].to_numpy())

    def _eval(diff):
        if favorable == "lower":
            diff = -diff
        if diff > 0:
            return "超额达标" if favorable == "higher" else "节约"
        if diff < 0:
            return "未达标" if favorable == "higher" else "超支"
        return "持平"
    g["评价"] = g["差异"].apply(_eval)

    sheet = params.get("sheet_name") or "预算差异分析"
    xcol = group_col if (group_col and group_col in g.columns) else g.columns[0]
    chart = {"type": "bar", "x": xcol, "y": [budget_col, actual_col], "title": sheet}
    return str(sheet)[:31], g, chart


# ----- skill: RFM 客户分群 --------------------------------------------------

def skill_rfm_segment(cdf, params, metadata_rows, metadata_columns):
    """
    RFM 客户分群(Recency 最近 / Frequency 频次 / Monetary 金额)。
    params:
      customer_col : 客户标识(meta 列)
      recency_col  : 最近一次距今天数(越小越好,encrypted)
      frequency_col: 购买频次(越大越好)
      monetary_col : 消费金额(越大越好)
    输出:客户 R F M R得分 F得分 M得分 RFM 分群
    """
    import pandas as pd

    cust = params["customer_col"]
    r_col = params["recency_col"]
    f_col = params["frequency_col"]
    m_col = params["monetary_col"]

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)
    for c in (cust, r_col, f_col, m_col):
        if c not in full.columns:
            raise ValueError(f"rfm_segment: 列「{c}」不存在")

    df = full.groupby(cust).agg({r_col: "min", f_col: "sum", m_col: "sum"}).reset_index()

    def _score(s, ascending):
        # 五分位打分 1-5;recency 越小越好 → ascending=True 给高分要反转
        try:
            q = pd.qcut(s.rank(method="first"), 5, labels=[1, 2, 3, 4, 5])
            sc = q.astype(int)
        except Exception:
            sc = pd.Series([3] * len(s), index=s.index)
        return (6 - sc) if ascending else sc

    df["R得分"] = _score(df[r_col], ascending=True)    # recency 小=好=高分
    df["F得分"] = _score(df[f_col], ascending=False)
    df["M得分"] = _score(df[m_col], ascending=False)
    df["RFM"] = df["R得分"].astype(str) + df["F得分"].astype(str) + df["M得分"].astype(str)

    def _seg(row):
        r, f, m = row["R得分"], row["F得分"], row["M得分"]
        if r >= 4 and f >= 4 and m >= 4:
            return "重要价值客户"
        if r >= 4 and (f >= 3 or m >= 3):
            return "重要发展客户"
        if r <= 2 and f >= 4 and m >= 4:
            return "重要挽留客户"
        if r <= 2 and f <= 2:
            return "流失客户"
        if r >= 3:
            return "一般保持客户"
        return "一般客户"
    df["分群"] = df.apply(_seg, axis=1)

    df = df.rename(columns={r_col: "最近(天)", f_col: "频次", m_col: "金额"})
    df = df.sort_values(["M得分", "F得分"], ascending=False).reset_index(drop=True)

    sheet = params.get("sheet_name") or "RFM客户分群"
    chart = {"type": "bar", "x": cust, "y": "金额", "title": sheet}
    return str(sheet)[:31], df, chart


# ----- skill: 账龄分析 ------------------------------------------------------

def skill_ar_aging(cdf, params, metadata_rows, metadata_columns):
    """
    应收账款账龄分析(按逾期天数分桶)。
    params:
      amount_col : 金额列(encrypted)
      age_col    : 账龄/逾期天数列(encrypted)
      group_col? : 可选维度(如客户/大区),不给=整体
      bins?      : 分桶边界(默认 [0,30,60,90,180])
    输出:[group] 各账龄桶金额 + 合计 + 逾期占比
    """
    import pandas as pd
    import numpy as np

    amount_col = params["amount_col"]
    age_col = params["age_col"]
    group_col = params.get("group_col") or None
    bins = params.get("bins") or [0, 30, 60, 90, 180]

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)
    for c in (amount_col, age_col):
        if c not in full.columns:
            raise ValueError(f"ar_aging: 列「{c}」不存在")

    # 前置「未到期」桶兜住 <起点 的账龄(负逾期天数=提前开票/未到期);末尾对 NaN 账龄兜「账龄未知」。
    # 否则这些行会被 pd.cut 判越界/NaN → 静默丢失,账龄表合计对不上应收总账(数据产品命门)。
    edges = [float("-inf")] + list(bins) + [float("inf")]
    labels = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        if lo == float("-inf"):
            labels.append("未到期")
        elif hi == float("inf"):
            labels.append(f"{int(lo)}天以上")
        else:
            labels.append(f"{int(lo)}-{int(hi)}天")
    full = full.copy()
    bucket = pd.cut(full[age_col], bins=edges, labels=labels, right=False, include_lowest=True)
    # 账龄本身缺失(NaN)但金额有效的行:单列「账龄未知」,不静默吞掉金额
    bucket = bucket.cat.add_categories("账龄未知").fillna("账龄未知")
    full["_账龄桶"] = bucket
    all_labels = labels + ["账龄未知"]

    grp_keys = [group_col] if (group_col and group_col in full.columns) else []
    piv = full.pivot_table(index=grp_keys or None, columns="_账龄桶",
                           values=amount_col, aggfunc="sum", observed=False).fillna(0)
    if grp_keys:
        piv = piv.reset_index()
    else:
        piv = piv.sum().to_frame().T
    # 兜底桶整列为 0(无未到期 / 无缺失账龄)→ 删掉,免得报表添空列
    for extra in ("未到期", "账龄未知"):
        if extra in piv.columns and (piv[extra] == 0).all():
            piv = piv.drop(columns=extra)
    # 合计 + 逾期占比:未到期 + 首个到期桶(当期)视作未逾期,其余为逾期
    bucket_cols = [c for c in all_labels if c in piv.columns]
    piv["合计"] = piv[bucket_cols].sum(axis=1)
    due_cols = [c for c in bucket_cols if c not in ("未到期", "账龄未知")]  # 已到期桶(账龄>=0)
    overdue = [c for c in due_cols[1:] if c in piv.columns]  # 首个到期桶=当期,不算逾期
    with np.errstate(divide="ignore", invalid="ignore"):
        piv["逾期占比"] = piv[overdue].sum(axis=1) / piv["合计"].replace(0, np.nan)
    piv["逾期占比"] = piv["逾期占比"].fillna(0)

    sheet = params.get("sheet_name") or "账龄分析"
    xcol = grp_keys[0] if grp_keys else None
    chart = ({"type": "bar", "x": xcol, "y": bucket_cols, "title": sheet} if xcol else None)
    return str(sheet)[:31], piv.reset_index(drop=True), chart


# ----- skill: 帕累托 / ABC 分析 ---------------------------------------------

def skill_pareto_abc(cdf, params, metadata_rows, metadata_columns):
    """
    帕累托(80/20)/ ABC 分类。
    params:
      label_col : 分析对象(物料/客户/产品,meta 列)
      value_col : 金额/销量(encrypted)
      a_cut?    : A 类累计阈值(默认 0.80) b_cut?(默认 0.95)
    输出:对象 值 占比 累计占比 ABC类
    """
    import pandas as pd
    import numpy as np

    label_col = params["label_col"]
    value_col = params["value_col"]
    a_cut = float(params.get("a_cut", 0.80))
    b_cut = float(params.get("b_cut", 0.95))

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)
    for c in (label_col, value_col):
        if c not in full.columns:
            raise ValueError(f"pareto_abc: 列「{c}」不存在")

    g = full.groupby(label_col)[value_col].sum().reset_index()
    # ABC/帕累托是「正贡献的 80/20」:≤0 的值(退货冲减 / 录入异常)不参与占比与累计——
    # 否则总额被负值拉小,正值累计占比会突破 100%。把它们单拎出来标「数据异常」置于表尾。
    pos = g[g[value_col] > 0].sort_values(value_col, ascending=False).reset_index(drop=True)
    bad = g[g[value_col] <= 0].sort_values(value_col).reset_index(drop=True)
    total = pos[value_col].sum() or 1.0
    pos["占比"] = pos[value_col] / total
    pos["累计占比"] = pos["占比"].cumsum()

    def _abc(cum):
        if cum <= a_cut:
            return "A"
        if cum <= b_cut:
            return "B"
        return "C"
    pos["ABC类"] = pos["累计占比"].apply(_abc)
    if len(bad):
        bad["占比"] = np.nan
        bad["累计占比"] = np.nan
        bad["ABC类"] = "数据异常"
    g = pd.concat([pos, bad], ignore_index=True)

    sheet = params.get("sheet_name") or "帕累托ABC分析"
    chart = {"type": "bar", "x": label_col, "y": value_col, "title": sheet}
    return str(sheet)[:31], g, chart


# ----- skill: 多维交叉透视 --------------------------------------------------

def skill_pivot_summary(cdf, params, metadata_rows, metadata_columns):
    """
    多维交叉透视表(行维 × 列维 → 聚合值)。
    params:
      row_col   : 行维度(meta 列,如 销售大区)
      col_col?  : 列维度(meta 列,如 产品线;不给=单维汇总)
      value_col : 数值列(encrypted)
      agg?      : sum(默认)/ mean / count / max / min
    输出:行维 × 列维 交叉表 + 行合计
    """
    import pandas as pd

    row_col = params["row_col"]
    col_col = params.get("col_col") or None
    value_col = params["value_col"]
    agg = (params.get("agg") or "sum").lower()

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)  # 按 params['filter'] 只看指定实体(产品/大区/客户…)
    for c in [row_col, value_col] + ([col_col] if col_col else []):
        if c not in full.columns:
            raise ValueError(f"pivot_summary: 列「{c}」不存在")

    piv = full.pivot_table(
        index=row_col, columns=col_col, values=value_col,
        aggfunc=agg, observed=False, fill_value=0,
    )
    piv = piv.reset_index()
    # 行合计
    num_cols = [c for c in piv.columns if c != row_col]
    if col_col and num_cols:
        piv["合计"] = piv[num_cols].sum(axis=1)

    sheet = params.get("sheet_name") or f"{row_col}×{col_col or value_col}透视"
    ycol = "合计" if "合计" in piv.columns else (num_cols[0] if num_cols else None)
    chart = ({"type": "bar", "x": row_col, "y": ycol, "title": sheet} if ycol else None)
    return str(sheet)[:31], piv, chart


# ----- skill: 库存周转天数(DIO)+ 呆滞档位 ----------------------------------

def skill_inventory_turnover(cdf, params, metadata_rows, metadata_columns):
    """
    库存周转天数 DIO = 平均库存 ÷ 期间销货成本 × 天数;并按天数分呆滞/正常档位。
    params:
      item_col  : 物料/SKU(meta 列)
      stock_col : 库存(金额或数量,encrypted)—— 作为"平均库存"
      cogs_col  : 销货成本 / 出库(encrypted)
      days?     : 期间天数(默认 365)
      slow_days?: 呆滞阈值(默认 90);正常 ≤ warn_days,warn_days?默认 60
    输出:物料 库存 销货成本 周转天数 库存状态(正常/关注/呆滞)
    """
    import pandas as pd
    import numpy as np

    item = params["item_col"]
    stock = params["stock_col"]
    cogs = params["cogs_col"]
    days = float(params.get("days", 365))
    warn_days = float(params.get("warn_days", 60))
    slow_days = float(params.get("slow_days", 90))

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)
    for c in (item, stock, cogs):
        if c not in full.columns:
            raise ValueError(f"inventory_turnover: 列「{c}」不存在")

    g = full.groupby(item, as_index=False).agg(**{stock: (stock, "mean"), cogs: (cogs, "sum")})
    g["周转天数"] = _inf_to_nan(g[stock] * days / g[cogs].replace(0, np.nan))

    def _tier(row):
        s, c, d = row[stock], row[cogs], row["周转天数"]
        # 负库存(退货冲减)/负销货成本(净入库)→ 数据异常,单列出来别混进正常档
        if (s == s and s < 0) or (c == c and c < 0):
            return "数据异常"
        if d != d:            # NaN(无销货成本→无法周转,视为呆滞)
            return "呆滞"
        if d <= warn_days:
            return "正常"
        return "关注" if d <= slow_days else "呆滞"
    g["库存状态"] = g.apply(_tier, axis=1)
    # 排序用显式键,不靠 NaN 位置:零销货成本的呆滞(NaN=不周转)视为最滞→∞置顶;
    # 数据异常(负值)→ -∞ 一律置尾,并把其无意义的负周转天数置空,免得财务误读。
    is_bad = g["库存状态"] == "数据异常"
    sort_key = g["周转天数"].fillna(np.inf).where(~is_bad, -np.inf)
    g = g.assign(_k=sort_key).sort_values("_k", ascending=False).drop(columns="_k").reset_index(drop=True)
    g.loc[g["库存状态"] == "数据异常", "周转天数"] = np.nan

    sheet = params.get("sheet_name") or "库存周转分析"
    chart = {"type": "bar", "x": item, "y": "周转天数", "title": sheet}
    return str(sheet)[:31], g, chart


# ----- skill: HR 绩效分级(优/良/中/差)-------------------------------------

def skill_hr_grade(cdf, params, metadata_rows, metadata_columns):
    """
    按某绩效指标给员工分级(默认按分位:优 top20% / 良 20-50% / 中 50-80% / 差 bottom20%)。
    (人均产出等人效口径见独立技能 per_capita。)
    params:
      name_col   : 姓名/工号(meta 列)
      metric_col : 绩效指标(encrypted,如 绩效得分/销售额/完成率)
      group_col? : 部门/大区(meta 列)—— 给了则**组内**分位分级
      cuts?      : 分位切点(默认 [0.2,0.5,0.8]),对应 差|中|良|优
    输出:姓名 [部门] 指标 绩效等级(优/良/中/差)
    """
    import pandas as pd

    name = params["name_col"]
    metric = params["metric_col"]
    group = params.get("group_col") or None
    labels = ["差", "中", "良", "优"]
    cuts = params.get("cuts") or [0.2, 0.5, 0.8]
    # 校验 cuts:必须恰好 len(labels)-1 个、严格递增、落在 (0,1) —— 否则退回默认,不让越界崩溃
    try:
        cuts = [float(x) for x in cuts]
        ok = (len(cuts) == len(labels) - 1
              and all(0 < a < b < 1 for a, b in zip(cuts, cuts[1:]))
              and 0 < cuts[0] < 1 and 0 < cuts[-1] < 1)
        if not ok:
            cuts = [0.2, 0.5, 0.8]
    except (TypeError, ValueError):
        cuts = [0.2, 0.5, 0.8]

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)
    for c in [name, metric] + ([group] if group else []):
        if c not in full.columns:
            raise ValueError(f"hr_grade: 列「{c}」不存在")

    def _grade_series(s):
        # 按分位打档;指标越高越好。全员同分(或单人)→ 分位无意义,统一给"中"不误标"优"
        if s.nunique(dropna=True) <= 1:
            return pd.Series("中", index=s.index)
        q = s.rank(pct=True, method="average")
        edges = [0.0] + list(cuts) + [1.0]
        out = pd.Series(labels[-1], index=s.index)
        for i in range(len(labels)):
            lo, hi = edges[i], edges[i + 1]
            mask = (q > lo) & (q <= hi) if i > 0 else (q <= hi)
            out[mask] = labels[i]
        return out

    full = full.copy()
    if group:
        full["绩效等级"] = full.groupby(group)[metric].transform(_grade_series)
    else:
        full["绩效等级"] = _grade_series(full[metric])

    keep = ([group] if group else []) + [name, metric, "绩效等级"]
    out = full[keep]
    # 排序:组内(或整体)按指标降序,好的在前
    sort_cols = ([group] if group else []) + [metric]
    out = out.sort_values(sort_cols, ascending=[True] * (1 if group else 0) + [False])

    sheet = params.get("sheet_name") or "绩效分级"
    chart = {"type": "bar", "x": name, "y": metric, "title": sheet,
             "split_by": group if group else None}
    return str(sheet)[:31], out.reset_index(drop=True), chart


# ----- skill: 人效 / 人均分析(人均产出·人均利润·人均成本)---------------------

def skill_per_capita(cdf, params, metadata_rows, metadata_columns):
    """
    人效/人均:按维度(部门/大区)算 人均指标 = 指标总和 ÷ 人数。
    params:
      group_col   : 分组维度(部门/大区,meta 列)
      value_col   : 产出 / 利润 / 成本(encrypted)
      name_col?   : 姓名/工号(meta 列)—— 给了则人数=去重人头;不给=按行数计人数
      metric_name?: 人均列名(默认「人均+value_col」)
      ascending?  : 排序方向(默认降序,人效高的在前)
    输出:维度 指标总和 人数 人均指标 (+合计行;合计人均=总额÷总人数,加权口径)
    """
    import pandas as pd
    import numpy as np

    group = params["group_col"]
    value = params["value_col"]
    name = params.get("name_col") or None
    ascending = bool(params.get("ascending", False))
    metric_name = params.get("metric_name") or f"人均{value}"

    decrypted = _decrypt(cdf)
    full = _merge_meta(decrypted, metadata_rows, metadata_columns)
    full = _apply_filter(full, params)
    for c in [group, value] + ([name] if name else []):
        if c not in full.columns:
            raise ValueError(f"per_capita: 列「{c}」不存在")

    # 人数:给了姓名列 → 组内去重人头(同一人多行记录不重复计数);否则按行数
    if name:
        agg = full.groupby(group).agg(**{"指标总和": (value, "sum"),
                                         "人数": (name, "nunique")}).reset_index()
    else:
        agg = full.groupby(group).agg(**{"指标总和": (value, "sum"),
                                         "人数": (value, "size")}).reset_index()
    agg[metric_name] = _inf_to_nan(agg["指标总和"] / agg["人数"].replace(0, np.nan))
    agg = agg.sort_values(metric_name, ascending=ascending,
                          na_position="last").reset_index(drop=True)

    # 合计行:人均按「总额 ÷ 总人数」加权,不是各部门人均的简单平均(否则大部门被稀释)
    total_val = agg["指标总和"].sum()
    total_head = agg["人数"].sum()
    total_row = {group: "合计", "指标总和": total_val, "人数": total_head,
                 metric_name: (total_val / total_head if total_head else np.nan)}
    agg = pd.concat([agg, pd.DataFrame([total_row])], ignore_index=True)

    sheet = params.get("sheet_name") or "人效分析"
    chart = {"type": "bar", "x": group, "y": metric_name, "title": sheet}
    return str(sheet)[:31], agg, chart


# ---------------------------------------------------------------------------
# Skill 注册表 — LLM 必须从这里选
# ---------------------------------------------------------------------------

SKILLS: dict[str, dict[str, Any]] = {
    "ratio_by_group": {
        "tool": "pandaseal",
        "fn": skill_ratio_by_group,
        "desc": "按维度分组算每组 sum(num)/sum(den) 比率(基数加权 · 回款率/完成率/库存周转)",
        "params": ["num_col", "den_col", "group_col", "metric_name", "ascending", "filter", "sheet_name"],
        "note": "口径:加权比率 = 组内分子总和 ÷ 分母总和(按基数加权,大单影响大)",
    },
    "row_ratio_then_group_mean": {
        "tool": "pandaseal",
        "fn": skill_row_ratio_then_group_mean,
        "desc": "先算每行 num/den 行级率,再按维度取均值(等权平均率)",
        "params": ["num_col", "den_col", "group_col", "metric_name", "ascending", "filter", "sheet_name"],
        "note": "口径:等权平均 = 各行比率的简单平均(每行权重相同,与加权口径结果可能差异很大)",
    },
    "top_n_by": {
        "tool": "pandaseal",
        "fn": skill_top_n_by,
        "desc": "按值取 TOP / BOTTOM N(ascending=true 为 BOTTOM)· 带身份列",
        "params": ["value_col", "n", "ascending", "filter", "sheet_name"],
    },
    "group_stats": {
        "tool": "pandaseal",
        "fn": skill_group_stats,
        "desc": "按维度分组,对多个数字列算多个聚合(mean/max/min/count/sum/std)",
        "params": ["group_col", "value_cols", "aggs", "filter", "sheet_name"],
    },
    "describe": {
        "tool": "pandaseal",
        "fn": skill_describe,
        "desc": "整体描述统计 count/mean/std/min/max",
        "params": ["value_cols", "filter", "sheet_name"],
    },
    "row_detail": {
        "tool": "pandaseal",
        "fn": skill_row_detail,
        "desc": "逐行明细 + 可选派生比率列 · 适合「展示每位员工目标完成率」",
        "params": ["value_cols", "compute", "sort_by", "ascending", "n", "filter", "sheet_name"],
    },
    "forecast_linreg": {
        "tool": "pandaseal+henumpy+helearn",
        "fn": skill_forecast_linreg,
        "desc": "时间序列预测 · pandas 清洗 → henumpy 加密数组 → helearn HE 线性回归 fit+predict",
        "params": ["value_col", "time_col", "group_col", "n_periods", "agg",
                   "iterations", "learning_rate", "filter", "sheet_name"],
    },
    # ---- 业务分析(第一梯队) ----
    "yoy_mom": {
        "tool": "pandaseal",
        "fn": skill_yoy_mom,
        "desc": "同比(YoY)/ 环比(MoM)增长分析 · 月度/年度增长额 + 增长率",
        "params": ["time_col", "value_col", "group_col", "mode", "agg", "filter", "sheet_name"],
    },
    "budget_variance": {
        "tool": "pandaseal",
        "fn": skill_budget_variance,
        "desc": "预算 vs 实际差异分析 · 差异额 + 差异率 + 超支/节约评价",
        "params": ["budget_col", "actual_col", "group_col", "favorable", "filter", "sheet_name"],
    },
    "rfm_segment": {
        "tool": "pandaseal",
        "fn": skill_rfm_segment,
        "desc": "RFM 客户分群 · 最近/频次/金额五分位打分 → 重要价值/挽留/流失等分群",
        "params": ["customer_col", "recency_col", "frequency_col", "monetary_col", "filter", "sheet_name"],
    },
    "ar_aging": {
        "tool": "pandaseal",
        "fn": skill_ar_aging,
        "desc": "应收账款账龄分析 · 按逾期天数分桶(0-30/30-60/...)+ 逾期占比",
        "params": ["amount_col", "age_col", "group_col", "bins", "filter", "sheet_name"],
    },
    "pareto_abc": {
        "tool": "pandaseal",
        "fn": skill_pareto_abc,
        "desc": "帕累托(80/20)/ ABC 分类 · 按金额降序累计占比分 A/B/C 三类",
        "params": ["label_col", "value_col", "a_cut", "b_cut", "filter", "sheet_name"],
    },
    "pivot_summary": {
        "tool": "pandaseal",
        "fn": skill_pivot_summary,
        "desc": "多维交叉透视(行维 × 列维 → 聚合)· 如 大区×产品线 销售额",
        "params": ["row_col", "col_col", "value_col", "agg", "filter", "sheet_name"],
    },
    "inventory_turnover": {
        "tool": "pandaseal",
        "fn": skill_inventory_turnover,
        "desc": "库存周转天数(DIO=平均库存÷销货成本×天数)+ 正常/关注/呆滞档位",
        "params": ["item_col", "stock_col", "cogs_col", "days", "warn_days", "slow_days", "filter", "sheet_name"],
        "note": "口径:周转天数 = 平均库存 ÷ 期间销货成本 × 天数;无销货成本的物料判为呆滞",
    },
    "hr_grade": {
        "tool": "pandaseal",
        "fn": skill_hr_grade,
        "desc": "HR 绩效分级(按分位分 优/良/中/差)· 可组内(部门)分级 · 逐人明细",
        "params": ["name_col", "metric_col", "group_col", "cuts", "filter", "sheet_name"],
        "note": "口径:按绩效指标分位分档(优top20%/良20-50%/中50-80%/差bottom20%),指标越高越好",
    },
    "per_capita": {
        "tool": "pandaseal",
        "fn": skill_per_capita,
        "desc": "人效/人均分析:按部门/大区算 人均指标=指标总和÷人数(人均产出/利润/成本)· 带加权合计",
        "params": ["group_col", "value_col", "name_col", "metric_name", "ascending", "filter", "sheet_name"],
        "note": "口径:人均=组内指标总和÷人数(给姓名列则去重人头);合计行人均=总额÷总人数(加权,非部门人均的平均)",
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
