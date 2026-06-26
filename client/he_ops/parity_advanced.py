"""
进阶算子对拍 —— Block B / Phase B2:窗口/时序(window.py)+ 多条件(synth 布尔代数)。

每个场景:同一份明文,numpy/pandas 算参照 + 密文上跑、解密,比误差。
精确类 tol 1e-2;pct_change 含密态除法,走近似 tol 5e-2。

用法:AGENT_BACKEND=real python -m client.he_ops.parity_advanced [--save]
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from client.he_ops import synth, window


@dataclass
class AdvOp:
    id: str
    he: Callable     # (hp, c) -> 密文结果
    ref: Callable    # (np, a) -> 明文参照
    note: str
    tol_abs: float = 1e-2
    lo: float = 1.0  # 采样下界(pct_change 需远离 0)
    hi: float = 100.0


def _ops():
    gt, lt = synth.gt_threshold, synth.lt_threshold
    return [
        # ---- 窗口/时序 ----
        AdvOp("diff", lambda hp, c: window.diff(hp, c, 1),
              lambda np, a: np.diff(a), "1 阶差分(环比差额)"),
        AdvOp("diff2", lambda hp, c: window.diff(hp, c, 2),
              lambda np, a: a[2:] - a[:-2], "2 阶差分"),
        AdvOp("lag", lambda hp, c: window.lag(hp, c, 1),
              lambda np, a: a[:-1], "滞后对齐(上一期)"),
        AdvOp("rolling_sum", lambda hp, c: window.rolling_sum(hp, c, 3),
              lambda np, a: np.convolve(a, np.ones(3), "valid"), "移动求和 w=3"),
        AdvOp("rolling_mean", lambda hp, c: window.rolling_mean(hp, c, 3),
              lambda np, a: np.convolve(a, np.ones(3), "valid") / 3.0, "移动平均 w=3"),
        AdvOp("pct_change", lambda hp, c: window.pct_change(hp, c, 1),
              lambda np, a: (a[1:] - a[:-1]) / a[:-1], "变化率(近似/密态除)",
              tol_abs=5e-2, lo=20.0),
        # ---- 多条件(布尔代数)----
        AdvOp("sumif_and", lambda hp, c: synth.sumif_and(hp, c, [gt(hp, c, 30.0), lt(hp, c, 70.0)]),
              lambda np, a: float(np.sum(a * ((a > 30) & (a < 70)))), "30<a<70 求和(AND)"),
        AdvOp("sumif_or", lambda hp, c: synth.sumif_or(hp, c, [lt(hp, c, 20.0), gt(hp, c, 80.0)]),
              lambda np, a: float(np.sum(a * ((a < 20) | (a > 80)))), "a<20 或 a>80 求和(OR)"),
        AdvOp("countif_and", lambda hp, c: synth.countif_and(hp, [gt(hp, c, 30.0), lt(hp, c, 70.0)]),
              lambda np, a: float(np.sum((a > 30) & (a < 70))), "30<a<70 计数(AND)"),
        AdvOp("countif_or", lambda hp, c: synth.countif_or(hp, [lt(hp, c, 20.0), gt(hp, c, 80.0)]),
              lambda np, a: float(np.sum((a < 20) | (a > 80))), "a<20 或 a>80 计数(OR)"),
        AdvOp("bnot", lambda hp, c: synth.sum_masked(hp, c, synth.bnot(hp, gt(hp, c, 50.0))),
              lambda np, a: float(np.sum(a * ~(a > 50))), "NOT(a>50) 求和 = sum(a<=50)"),
    ]


@dataclass
class AdvResult:
    op_id: str
    passed: bool
    max_abs_err: float
    secs: float
    error: str = ""


def _flat(x, np):
    import numpy as _np
    return _np.asarray(x, dtype=float).ravel()


def run_op(op: AdvOp, n: int = 12) -> AdvResult:
    import numpy as np
    import crypto_toolkit as ct
    import henumpy as hp
    from client.tools.runtime import Runtime

    t0 = time.time()
    try:
        Runtime.get().ensure_all_initialized()
        np.random.seed(abs(hash(op.id)) % (2**32))
        a = np.round(np.random.uniform(op.lo, op.hi, n), 2)
        ref = _flat(op.ref(np, a), np)
        got = _flat(ct.decrypt(op.he(hp, ct.encrypt(a))), np)
        if ref.size != got.size:
            return AdvResult(op.id, False, float("inf"), round(time.time() - t0, 2),
                             f"shape {ref.size}≠{got.size}")
        err = float(np.max(np.abs(ref - got))) if ref.size else 0.0
        return AdvResult(op.id, err <= op.tol_abs, err, round(time.time() - t0, 2))
    except Exception as e:  # noqa: BLE001
        return AdvResult(op.id, False, float("nan"), round(time.time() - t0, 2),
                         f"{type(e).__name__}: {e}")


def run_all():
    return [run_op(o) for o in _ops()]


REPORT = Path(__file__).resolve().parent / "parity_advanced_report.json"


def _print(results):
    print(f"{'进阶算子':<14}{'通过':<5}{'max_abs':<12}{'秒':<6}备注")
    print("-" * 64)
    ok = 0
    notes = {o.id: o.note for o in _ops()}
    for r in results:
        f = "✓" if r.passed else "✗"
        ok += r.passed
        ae = "—" if r.max_abs_err != r.max_abs_err else f"{r.max_abs_err:.2e}"
        print(f"{r.op_id:<14}{f:<5}{ae:<12}{r.secs:<6}{r.error or notes.get(r.op_id,'')}")
    print("-" * 64)
    print(f"通过 {ok}/{len(results)}")


if __name__ == "__main__":
    import json
    from dataclasses import asdict
    res = run_all()
    _print(res)
    if "--save" in sys.argv:
        REPORT.write_text(json.dumps({r.op_id: asdict(r) for r in res}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"已写入进阶对拍报告 → {REPORT.name}")
