"""
有效输入域剖面 —— Block C(数值硬化)。

近似算子(exp/log/sqrt/reciprocal/div)是多项式近似,**只在有限输入域内可靠**,
域外误差发散。本件对每个近似算子扫不同输入量级,量化"误差 vs 输入范围",
定出**有效域护栏**,供 codegen/capability_brief 提示 LLM:数据超域要先归一化/换路。

做法:对每个算子的一组候选输入区间,各采样、密文求值、解密,与 numpy 比相对误差;
相对误差 ≤ 预算(1e-2)的区间即"可靠域"。

用法:AGENT_BACKEND=real python -m client.he_ops.parity_domain [--save]
"""
from __future__ import annotations

import sys
from pathlib import Path

REL_BUDGET = 1e-2

# 每个近似算子:候选输入区间(从窄到宽/从典型到极端)
PROFILES = {
    "exp": [(-1, 1), (-3, 3), (-5, 5), (0, 8), (0, 15)],
    "log": [(0.5, 2), (0.1, 1), (1, 10), (1, 100), (1e-3, 1)],
    "sqrt": [(0.5, 2), (0.01, 1), (1, 100), (1, 1e4), (1, 1e6)],
    "reciprocal": [(0.5, 5), (1, 100), (0.1, 1), (0.01, 0.5)],
}
_FN = {
    "exp": ("exp", lambda np, a: np.exp(a)),
    "log": ("log", lambda np, a: np.log(a)),
    "sqrt": ("sqrt", lambda np, a: np.sqrt(a)),
    "reciprocal": ("reciprocal", lambda np, a: np.reciprocal(a)),
}


def _rel_err(op: str, lo: float, hi: float, n: int = 8) -> float:
    import numpy as np
    import crypto_toolkit as ct
    import henumpy as hp
    from client.tools.runtime import Runtime

    Runtime.get().ensure_all_initialized()
    np.random.seed(abs(hash((op, lo, hi))) % (2**32))
    a = np.random.uniform(lo, hi, n)
    ref = _FN[op][1](np, a)
    got = np.ravel(np.asarray(ct.decrypt(getattr(hp, _FN[op][0])(ct.encrypt(a)))))
    return float(np.max(np.abs(got - ref) / np.maximum(np.abs(ref), 1e-9)))


def run_all() -> dict:
    out = {}
    for op, ranges in PROFILES.items():
        rows = []
        for lo, hi in ranges:
            try:
                rel = _rel_err(op, lo, hi)
                rows.append({"range": [lo, hi], "max_rel_err": rel, "ok": rel <= REL_BUDGET})
            except Exception as e:  # noqa: BLE001
                rows.append({"range": [lo, hi], "max_rel_err": float("nan"), "ok": False,
                             "error": f"{type(e).__name__}: {e}"})
        ok_ranges = [r["range"] for r in rows if r["ok"]]
        out[op] = {"rows": rows, "reliable_ranges": ok_ranges}
    return out


def domain_brief(profile: dict) -> str:
    """一行/算子的可靠域摘要,供注入提示。"""
    parts = []
    for op, info in profile.items():
        rr = info["reliable_ranges"]
        if rr:
            lo = min(r[0] for r in rr); hi = max(r[1] for r in rr)
            parts.append(f"{op}∈[{lo:g},{hi:g}]")
        else:
            parts.append(f"{op}:均超差")
    return "近似算子可靠输入域(超出先归一化):" + "; ".join(parts)


REPORT = Path(__file__).resolve().parent / "parity_domain_report.json"


def _print(profile: dict):
    for op, info in profile.items():
        print(f"\n[{op}]  可靠域: {info['reliable_ranges'] or '无'}")
        for r in info["rows"]:
            ae = "—" if r["max_rel_err"] != r["max_rel_err"] else f"{r['max_rel_err']:.2e}"
            print(f"   {str(r['range']):<16} rel={ae:<10} {'✓' if r['ok'] else '✗'} {r.get('error','')}")
    print("\n" + domain_brief(profile))


if __name__ == "__main__":
    import json
    prof = run_all()
    _print(prof)
    if "--save" in sys.argv:
        REPORT.write_text(json.dumps(prof, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n已写入有效域报告 → {REPORT.name}")
