"""
表级对拍(pandaseal)。

把"明文 pandas 操作"与"pandaseal 在密文 DataFrame 上的同款操作"对拍:同一份表,
pandas 算参照 + pandaseal 在 ct.encrypt_df(df) 上算、解密,比误差。
覆盖真实分析最常用的列运算 / 聚合 / 排序 / 比较。

注意:pandaseal 的 std/var 用 pandas 语义(ddof=1),故参照一律用 pandas(不是 numpy)。

用法:AGENT_BACKEND=real python -m client.he_ops.parity_df [--save] [op_id ...]
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class DfOp:
    id: str
    he: Callable     # (cdf) -> 密文结果(CipherDataFrame/Series/Array)或已是明文(如 .gt)
    ref: Callable    # (df)  -> pandas 参照
    note: str = ""
    tol_abs: float = 1e-2


DF_OPS: list[DfOp] = [
    DfOp("col_add", lambda cdf: cdf["a"] + cdf["b"], lambda df: df["a"] + df["b"],
         "两列相加(派生指标基础)。"),
    DfOp("col_sub", lambda cdf: cdf["a"] - cdf["b"], lambda df: df["a"] - df["b"], "两列相减/差额。"),
    DfOp("col_mul", lambda cdf: cdf["a"] * cdf["b"], lambda df: df["a"] * df["b"], "两列相乘。"),
    DfOp("col_div", lambda cdf: cdf["a"] / cdf["b"], lambda df: df["a"] / df["b"], "两列相除(回款率/占比)。"),
    DfOp("col_sum", lambda cdf: cdf["a"].sum(), lambda df: df["a"].sum(), "列求和。"),
    DfOp("col_mean", lambda cdf: cdf["a"].mean(), lambda df: df["a"].mean(), "列均值。"),
    DfOp("df_mean", lambda cdf: cdf.mean(), lambda df: df.mean(), "整表逐列均值。"),
    DfOp("df_var", lambda cdf: cdf.var(), lambda df: df.var(), "逐列方差(ddof=1,pandas 语义)。"),
    DfOp("df_std", lambda cdf: cdf.std(), lambda df: df.std(), "逐列标准差。"),
    DfOp("df_cumsum", lambda cdf: cdf.cumsum(), lambda df: df.cumsum(), "逐列累计。"),
    DfOp("df_max", lambda cdf: cdf.max(), lambda df: df.max(), "逐列最大值。"),
    DfOp("df_quantile", lambda cdf: cdf.quantile(0.5), lambda df: df.quantile(0.5), "中位数/分位数。"),
    DfOp("sort_values", lambda cdf: cdf.sort_values("a"), lambda df: df.sort_values("a"), "按列排序(排名基础)。"),
    DfOp("col_gt", lambda cdf: cdf["a"].gt(cdf["b"]), lambda df: df["a"].gt(df["b"]),
         "列比较 a>b(pandaseal 原生,返回布尔)。"),
]


def _decrypt(r, ct):
    tn = type(r).__name__
    if tn in ("CipherDataFrame", "CipherSeries"):
        return ct.decrypt_df(r)
    if tn == "CipherArray":
        return ct.decrypt(r)
    return r   # 已是明文(.gt 返回 bool Series 等)


def _to_values(x, np, pd):
    if isinstance(x, (pd.DataFrame, pd.Series)):
        return x.to_numpy(dtype=float).ravel()
    return np.asarray(x, dtype=float).ravel()


@dataclass
class DfResult:
    op_id: str
    passed: bool
    max_abs_err: float
    secs: float
    error: str = ""


def run_op(op: DfOp, n: int = 6) -> DfResult:
    import numpy as np
    import pandas as pd
    import crypto_toolkit as ct
    from client.tools.runtime import Runtime

    t0 = time.time()
    try:
        Runtime.get().ensure_all_initialized()
        np.random.seed(abs(hash(op.id)) % (2**32))
        df = pd.DataFrame({
            "a": np.round(np.random.uniform(1, 50, n), 2),
            "b": np.round(np.random.uniform(1, 50, n), 2),
        })
        ref = op.ref(df)
        cdf = ct.encrypt_df(df)
        got = _decrypt(op.he(cdf), ct)
        pv, gv = _to_values(ref, np, pd), _to_values(got, np, pd)
        if pv.size != gv.size:
            return DfResult(op.id, False, float("inf"), round(time.time() - t0, 2),
                            f"shape {pv.size}≠{gv.size}")
        # 排序类:两边都排过序,逐元素比;比较类:bool→0/1
        err = float(np.max(np.abs(np.sort(pv) - np.sort(gv)))) if op.id == "sort_values" \
            else float(np.max(np.abs(pv - gv)))
        return DfResult(op.id, err <= op.tol_abs, err, round(time.time() - t0, 2))
    except Exception as e:  # noqa: BLE001
        return DfResult(op.id, False, float("nan"), round(time.time() - t0, 2),
                        f"{type(e).__name__}: {e}")


def run_all(ids=None, n=6):
    ops = [o for o in DF_OPS if (not ids or o.id in ids)]
    return [run_op(o, n) for o in ops]


REPORT = Path(__file__).resolve().parent / "parity_df_report.json"


def _print(results):
    print(f"{'表级算子':<16}{'通过':<5}{'max_abs':<12}{'秒':<6}备注")
    print("-" * 70)
    ok = 0
    for r in results:
        f = "✓" if r.passed else "✗"
        ok += r.passed
        ae = "—" if r.max_abs_err != r.max_abs_err else f"{r.max_abs_err:.2e}"
        print(f"{r.op_id:<16}{f:<5}{ae:<12}{r.secs:<6}{r.error}")
    print("-" * 70)
    print(f"通过 {ok}/{len(results)}")


if __name__ == "__main__":
    import json
    from dataclasses import asdict
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    res = run_all(args or None)
    _print(res)
    if "--save" in sys.argv:
        REPORT.write_text(json.dumps({r.op_id: asdict(r) for r in res}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"已写入表级对拍报告 → {REPORT.name}")
