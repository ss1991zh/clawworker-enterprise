"""
数据健康体检 + 清洗 —— Phase D:真实脏数据的容错。

真实业务表常见脏:数值列里混 "N/A"/"-"/"暂无" → pandas 判成 object(文本)→ 该列被当身份列、
不参与密态计算(用户却以为算了);空值、inf、常量列、超大表也要识别并给说明。

本模块两件事:
  · column_report(name, series)  逐列体检:类型、空值、数值可强转比例、inf、常量、脏值数。
  · clean_for_encryption(df)     按"非空值里数值可强转 ≥ 阈值"重新判定数值/身份列,
    把数值列里的脏值/空值标记转成 NaN,inf 转 NaN —— 让该加密的列真的能加密、能算。

隐私:只在**本机明文入库阶段**对用户自己的数据做,不外发、不进 LLM。
"""
from __future__ import annotations

# 常见"空值"文本标记(小写、去空格后匹配)→ 视为缺失
_NULL_MARKERS = {"", "-", "--", "/", "\\", ".", "n/a", "na", "null", "none", "nan",
                 "暂无", "无", "未知", "待定", "#n/a", "#value!", "—"}

NUMERIC_THRESHOLD = 0.6   # 非空值里 ≥60% 能转成数字 → 判为数值列


def _clean_series(s):
    """把空值标记替换成 NaN,返回清洗后的 Series(不改原列类型)。"""
    import pandas as pd

    def _norm(x):
        if x is None:
            return None
        if isinstance(x, str) and x.strip().lower() in _NULL_MARKERS:
            return None
        return x
    return s.map(_norm)


def column_report(name: str, s) -> dict:
    """逐列体检报告。"""
    import numpy as np
    import pandas as pd

    n = len(s)
    cleaned = _clean_series(s)
    nulls = int(cleaned.isna().sum())
    non_null = n - nulls
    coerced = pd.to_numeric(cleaned, errors="coerce")
    numeric_ok = int(coerced.notna().sum())
    # 数值可强转比例(基于非空)
    pct = (numeric_ok / non_null) if non_null else 0.0
    is_numeric = pct >= NUMERIC_THRESHOLD
    # 脏值:非空但转不成数字(只有当判为数值列时才算"脏")
    junk = (non_null - numeric_ok) if is_numeric else 0
    inf_count = int(np.isinf(coerced.to_numpy(dtype="float64", na_value=np.nan)).sum()) if is_numeric else 0
    constant = bool(non_null > 0 and cleaned.nunique(dropna=True) <= 1)
    notes = []
    if nulls:
        notes.append(f"空值 {nulls}")
    if junk:
        notes.append(f"非数字脏值 {junk}(已转空)")
    if inf_count:
        notes.append(f"inf {inf_count}(已转空)")
    if constant:
        notes.append("常量列")
    return {
        "name": str(name),
        "classified": "numeric" if is_numeric else "identity",
        "rows": n, "nulls": nulls, "null_pct": round(nulls / n, 3) if n else 0,
        "numeric_coercible_pct": round(pct, 3),
        "junk_values": junk, "inf": inf_count, "constant": constant,
        "note": " · ".join(notes) or "干净",
    }


def clean_for_encryption(df, threshold: float = NUMERIC_THRESHOLD):
    """按体检结果重新判定列,并把数值列清洗成可加密形态(脏值/空标记/inf → NaN)。
    返回 (df_clean, numeric_cols, string_cols, reports)。df_clean 的数值列已是数值 dtype(含 NaN)。"""
    import numpy as np
    import pandas as pd

    reports = [column_report(c, df[c]) for c in df.columns]
    numeric_cols = [r["name"] for r in reports if r["classified"] == "numeric"]
    string_cols = [r["name"] for r in reports if r["classified"] == "identity"]

    out = df.copy()
    for c in numeric_cols:
        col = pd.to_numeric(_clean_series(df[c]), errors="coerce")
        out[c] = col.replace([np.inf, -np.inf], np.nan)
    # 身份列统一成字符串(空标记 → 空串,避免 NaN 进 metadata)
    for c in string_cols:
        out[c] = _clean_series(df[c]).astype("object").where(lambda x: x.notna(), "")
    return out, numeric_cols, string_cols, reports


def health_summary(reports: list[dict], n_rows: int) -> dict:
    """整表健康摘要(给用户/上传响应)。"""
    numeric = [r for r in reports if r["classified"] == "numeric"]
    dirty = [r for r in reports if r["note"] != "干净"]
    big = n_rows >= 500_000
    msgs = []
    if dirty:
        msgs.append(f"{len(dirty)} 列有脏值/空值(已自动清洗,不影响计算)")
    if big:
        msgs.append(f"大表 {n_rows} 行(密态聚合可平稳跑;排名/明细会走授权解密)")
    coerced_back = [r["name"] for r in reports
                    if r["classified"] == "numeric" and r["junk_values"]]
    if coerced_back:
        msgs.append(f"{len(coerced_back)} 个数值列原本混了文本,已强转为数值参与计算:{', '.join(coerced_back[:4])}")
    return {
        "rows": n_rows,
        "numeric_cols": len(numeric),
        "identity_cols": len(reports) - len(numeric),
        "dirty_cols": len(dirty),
        "big_table": big,
        "message": " · ".join(msgs) or "数据干净,无需清洗。",
    }
