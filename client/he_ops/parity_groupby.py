"""
密态 group-by 对拍 —— 验证 client/he_ops/groupby.py 对齐 pandas.groupby。

造一份"明文维度键 + 密文度量"的表,密态分组聚合 vs pandas 分组聚合,逐组比误差。
sum/mean/count 期望精确(tol 1e-2);max/min 为近似(走宽容差)。

用法:AGENT_BACKEND=real python -m client.he_ops.parity_groupby [--save]
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from client.he_ops import groupby as gb

AGGS = [("sum", 1e-2), ("mean", 1e-2), ("count", 0.0), ("max", 5e-2), ("min", 5e-2)]


@dataclass
class GbResult:
    agg: str
    passed: bool
    max_abs_err: float
    groups: int
    secs: float
    error: str = ""


def _decrypt_groups(res: dict, ct, np) -> dict:
    """{组: 密文标量|int} → {组: float}。"""
    out = {}
    for g, v in res.items():
        if isinstance(v, (int, float)):
            out[g] = float(v)
        else:
            out[g] = float(np.ravel(ct.decrypt(v))[0])
    return out


def run_agg(agg: str, tol: float, n: int = 24) -> GbResult:
    import numpy as np
    import pandas as pd
    import crypto_toolkit as ct
    import henumpy as hp
    from client.tools.runtime import Runtime

    t0 = time.time()
    try:
        Runtime.get().ensure_all_initialized()
        np.random.seed(abs(hash(agg)) % (2**32))
        keys = np.random.choice(["华东", "华北", "华南", "西部"], n)
        measure = np.round(np.random.uniform(1, 100, n), 2)
        # pandas 参照
        ref = pd.Series(measure).groupby(keys).agg(agg).to_dict()
        # 密态
        cmeasure = ct.encrypt(measure)
        got = _decrypt_groups(gb.groupby_agg(hp, cmeasure, list(keys), agg), ct, np)
        # 逐组比
        gs = sorted(ref.keys())
        if sorted(got.keys()) != gs:
            return GbResult(agg, False, float("inf"), len(gs), round(time.time() - t0, 2),
                            f"组集不一致 {sorted(got.keys())} vs {gs}")
        err = max(abs(got[g] - float(ref[g])) for g in gs)
        return GbResult(agg, err <= tol, err, len(gs), round(time.time() - t0, 2))
    except Exception as e:  # noqa: BLE001
        return GbResult(agg, False, float("nan"), 0, round(time.time() - t0, 2),
                        f"{type(e).__name__}: {e}")


def run_all():
    return [run_agg(a, t) for a, t in AGGS]


REPORT = Path(__file__).resolve().parent / "parity_groupby_report.json"


def _print(results):
    print(f"{'分组聚合':<10}{'通过':<5}{'max_abs':<12}{'组数':<6}{'秒':<6}备注")
    print("-" * 64)
    ok = 0
    for r in results:
        f = "✓" if r.passed else "✗"
        ok += r.passed
        ae = "—" if r.max_abs_err != r.max_abs_err else f"{r.max_abs_err:.2e}"
        print(f"{r.agg:<10}{f:<5}{ae:<12}{r.groups:<6}{r.secs:<6}{r.error}")
    print("-" * 64)
    print(f"通过 {ok}/{len(results)}")


if __name__ == "__main__":
    import json
    from dataclasses import asdict
    res = run_all()
    _print(res)
    if "--save" in sys.argv:
        REPORT.write_text(json.dumps({r.agg: asdict(r) for r in res}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"已写入分组对拍报告 → {REPORT.name}")
