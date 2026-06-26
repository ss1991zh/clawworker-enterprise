"""
深度×误差剖面 —— Block C(数值硬化)。

CKKS 类近似算术的误差随**乘法深度**累积。单算子对拍只测深度 1(~1e-15),
但链式分析(连乘、ML 迭代)会逐层放大误差。本件量化"误差 vs 乘法深度"曲线,
得到一个**可用深度护栏**(误差超阈值的深度),供 planner/verifier 预警。

做法:对深度 d,连乘 d 个 ~1 的值(uniform(0.9,1.1),保持量级稳定),密文逐层相乘、
解密,与 numpy 连乘比相对误差。纯乘法链是放大乘法深度噪声的最直接探针。

用法:AGENT_BACKEND=real python -m client.he_ops.parity_depth [--save]
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

DEPTHS = [1, 2, 4, 8, 16, 32]
REL_BUDGET = 1e-3   # 相对误差预算:超过即视为"深度不可用"


def run_depth(d: int, width: int = 8) -> dict:
    import numpy as np
    import crypto_toolkit as ct
    import henumpy as hp
    from client.tools.runtime import Runtime

    Runtime.get().ensure_all_initialized()
    np.random.seed(1000 + d)
    factors = [np.round(np.random.uniform(0.9, 1.1, width), 4) for _ in range(d)]
    ref = factors[0].copy()
    acc = ct.encrypt(factors[0])
    for f in factors[1:]:
        acc = hp.mul(acc, ct.encrypt(f))   # 每乘一次 +1 层乘法深度
        ref = ref * f
    got = np.ravel(np.asarray(ct.decrypt(acc)))
    rel = float(np.max(np.abs(got - ref) / np.maximum(np.abs(ref), 1e-9)))
    return {"depth": d, "max_rel_err": rel, "within_budget": rel <= REL_BUDGET}


def run_all() -> list[dict]:
    return [run_depth(d) for d in DEPTHS]


def usable_depth(profile: list[dict]) -> int:
    """误差仍在预算内的最大乘法深度(护栏值)。"""
    ok = [r["depth"] for r in profile if r["within_budget"]]
    return max(ok) if ok else 0


REPORT = Path(__file__).resolve().parent / "parity_depth_report.json"


def _print(profile):
    print(f"{'乘法深度':<10}{'max_rel_err':<14}{'预算内':<8}")
    print("-" * 36)
    for r in profile:
        print(f"{r['depth']:<10}{r['max_rel_err']:<14.2e}{'✓' if r['within_budget'] else '✗':<8}")
    print("-" * 36)
    print(f"可用乘法深度(rel≤{REL_BUDGET:g}):{usable_depth(profile)}")


if __name__ == "__main__":
    import json
    prof = run_all()
    _print(prof)
    if "--save" in sys.argv:
        REPORT.write_text(json.dumps({"budget": REL_BUDGET, "usable_depth": usable_depth(prof),
                                      "profile": prof}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已写入深度剖面报告 → {REPORT.name}")
