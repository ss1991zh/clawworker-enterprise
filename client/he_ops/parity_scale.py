"""
规模基准 / 百万级确认 —— Phase 0 + Phase 7 规模回归。

向量化密态聚合到底能不能"平稳"跑百万级?本件实测:对一组数据规模,测
encrypt / sum / group-by / sumif / rolling / decrypt(整列回环)的**时间 + 峰值内存 +
正确性**(对 numpy/pandas)。

前期探针(到 500k)显示:聚合线性、内存几乎不涨(库做 CKKS slot packing)。本件把
结论钉到 1M,并作为长期规模回归 + 导入体检的"规模档位"数据源。

注意:topk/rank 是 O(n²),**不在本基准里**(大表必须降级 decrypt-first,见 size-guard)。

用法:AGENT_BACKEND=real python -m client.he_ops.parity_scale [--save] [n1 n2 ...]
"""
from __future__ import annotations

import sys
import time
import resource
from dataclasses import dataclass, asdict, field
from pathlib import Path

DEFAULT_SIZES = [50_000, 200_000, 1_000_000]
REL_TOL = 1e-6   # 大数求和走相对误差


def _peak_rss_mb() -> float:
    # macOS: ru_maxrss 为字节;Linux: KB。统一粗略换算到 MB(用于趋势,不求精确)。
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1e6 if sys.platform == "darwin" else rss / 1e3


@dataclass
class ScaleResult:
    n: int
    t_encrypt: float
    t_sum: float
    t_groupby: float
    t_sumif: float
    t_rolling: float
    t_decrypt: float
    peak_rss_mb: float
    correct: bool
    max_rel_err: float
    error: str = ""


def run_size(n: int) -> ScaleResult:
    import numpy as np
    import pandas as pd
    import crypto_toolkit as ct
    import henumpy as hp
    from client.he_ops import groupby as gb, window as win, synth
    from client.tools.runtime import Runtime

    def tm(f):
        t = time.time(); r = f(); return time.time() - t, r

    try:
        Runtime.get().ensure_all_initialized()
        np.random.seed(12345)
        a = np.random.uniform(1, 100, n)
        keys = np.random.choice(list("ABCDE"), n)

        t_enc, c = tm(lambda: ct.encrypt(a))
        t_sum, csum = tm(lambda: hp.sum(c))
        t_gb, cgb = tm(lambda: gb.groupby_sum(hp, c, list(keys)))
        t_if, cif = tm(lambda: synth.sumif_gt(hp, c, 50.0))
        t_rl, _ = tm(lambda: win.rolling_mean(hp, c, 7))
        t_dec, dec = tm(lambda: np.ravel(np.asarray(ct.decrypt(c))))

        # 正确性(相对误差):sum / groupby / sumif / 解密回环
        errs = []
        errs.append(abs(float(ct.decrypt(csum)) - a.sum()) / max(abs(a.sum()), 1e-9))
        ref_gb = pd.Series(a).groupby(keys).sum().to_dict()
        for g, v in cgb.items():
            errs.append(abs(float(ct.decrypt(v)) - ref_gb[g]) / max(abs(ref_gb[g]), 1e-9))
        ref_if = float(a[a > 50].sum())
        errs.append(abs(float(ct.decrypt(cif)) - ref_if) / max(abs(ref_if), 1e-9))
        errs.append(float(np.max(np.abs(dec - a) / np.maximum(np.abs(a), 1e-9))))
        mre = float(max(errs))

        return ScaleResult(n, round(t_enc, 3), round(t_sum, 3), round(t_gb, 3),
                           round(t_if, 3), round(t_rl, 3), round(t_dec, 3),
                           round(_peak_rss_mb(), 0), mre <= REL_TOL, mre)
    except Exception as e:  # noqa: BLE001
        return ScaleResult(n, 0, 0, 0, 0, 0, 0, round(_peak_rss_mb(), 0), False, float("nan"),
                           f"{type(e).__name__}: {str(e)[:120]}")


def run_all(sizes=None):
    return [run_size(n) for n in (sizes or DEFAULT_SIZES)]


def scale_tier(results) -> dict:
    """给导入体检用的"规模档位":在容差内、且 group-by < 3s 的最大规模。"""
    ok = [r for r in results if r.correct and r.t_groupby < 3.0 and not r.error]
    return {
        "max_smooth_n": max((r.n for r in ok), default=0),
        "groupby_secs_at_max": next((r.t_groupby for r in reversed(ok)), None),
        "note": "超过此规模或做排名/topk,自动走授权解密(decrypt-first)。",
    }


REPORT = Path(__file__).resolve().parent / "parity_scale_report.json"


def _print(results):
    print(f"{'n':>9} {'enc':>7} {'sum':>7} {'groupby':>8} {'sumif':>7} {'rolling':>8} {'decrypt':>8} {'RSS_MB':>7} {'正确':>5} {'relerr':>9}")
    print("-" * 92)
    for r in results:
        flag = "✓" if r.correct else "✗"
        mre = "—" if r.max_rel_err != r.max_rel_err else f"{r.max_rel_err:.1e}"
        tail = f"  {r.error}" if r.error else ""
        print(f"{r.n:>9} {r.t_encrypt:>6.3f}s {r.t_sum:>6.3f}s {r.t_groupby:>7.3f}s {r.t_sumif:>6.3f}s "
              f"{r.t_rolling:>7.3f}s {r.t_decrypt:>7.3f}s {r.peak_rss_mb:>7.0f} {flag:>5} {mre:>9}{tail}")
    print("-" * 92)
    print("规模档位:", scale_tier(results))


if __name__ == "__main__":
    import json
    nums = [int(a) for a in sys.argv[1:] if a.isdigit()]
    res = run_all(nums or None)
    _print(res)
    if "--save" in sys.argv:
        REPORT.write_text(json.dumps({"results": [asdict(r) for r in res],
                                      "tier": scale_tier(res)}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        print(f"已写入规模基准报告 → {REPORT.name}")
