"""
明文↔密态对拍框架。

对每个算子:同一份随机数据,numpy 跑出参照 + henumpy 在密文上跑、解密,比对误差。
产出每个算子的实测 {max_abs_err, max_rel_err, passed},为能力表建立"可信度 + 真实精度"。

用法:
    AGENT_BACKEND=real python -m client.he_ops.parity            # 跑全部
    AGENT_BACKEND=real python -m client.he_ops.parity sum mean div   # 只跑指定算子
"""
from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from client.he_ops.registry import REGISTRY, Op, by_id


@dataclass
class PResult:
    op_id: str
    kind: str
    cost: str
    passed: bool
    max_abs_err: float
    max_rel_err: float
    secs: float
    error: str = ""


def _sample(op: Op, np, n: int) -> list:
    """按算子定义域采样 op.arity 个数组,规避 log 负数 / 除零 / exp 溢出等。"""
    u = np.random.uniform
    ranges = {
        "log": [(0.5, 5.0)],
        "sqrt": [(0.1, 9.0)],
        "exp": [(-1.0, 1.5)],
        "reciprocal": [(1.0, 5.0)],
        "div": [(1.0, 10.0), (1.0, 5.0)],
        "prod": [(0.7, 1.3)],          # 连乘防爆
    }
    if op.id in ranges:
        return [u(lo, hi, n) for (lo, hi) in ranges[op.id]]
    if op.category == "comparison" and op.arity == 2:
        return [u(1.0, 5.0, n), u(-5.0, 1.0, n)]
    # 边界/平局测试:把"恰好等于阈值"的值掺进数据,验证 >/>= 的 tie 语义(随机数据测不到)
    edges = {"gt_thr": [2.0], "between": [0.0, 3.0],
             "sumif_gt": [2.0], "countif_gt": [2.0], "bin_index": [0.0, 2.0, 4.0]}
    if op.id in edges and op.arity == 1:
        return [np.concatenate([np.array(edges[op.id], float), u(-3.0, 5.0, n)])]
    return [u(-3.0, 5.0, n) for _ in range(op.arity)]


def _flat(x, np):
    return np.asarray(x, dtype=np.float64).ravel()


def _compare(plain, got, np):
    p, g = _flat(plain, np), _flat(got, np)
    if p.size != g.size:
        return float("inf"), float("inf")
    if p.shape != g.shape:
        g = g.reshape(p.shape)
    diff = np.abs(p - g)
    abs_err = float(np.max(diff)) if diff.size else 0.0
    rel = diff / np.maximum(np.abs(p), 1e-9)
    rel_err = float(np.max(rel)) if rel.size else 0.0
    return abs_err, rel_err


class _Env:
    """传给算子 he() 的环境:像 henumpy 一样用(属性委托),并多一个 enc() 用于加密常量(阈值/分箱点等)。"""
    def __init__(self, hp, ct, np):
        self._hp, self.ct, self.np = hp, ct, np

    def __getattr__(self, k):
        return getattr(self._hp, k)   # env.add -> henumpy.add

    def enc(self, vals):
        return self.ct.encrypt(self.np.asarray(vals, dtype=self.np.float64).ravel())


def run_op(op: Op, n: int = 6) -> PResult:
    import numpy as np
    import crypto_toolkit as ct
    import henumpy as hp
    from client.tools.runtime import Runtime

    t0 = time.time()
    try:
        Runtime.get().ensure_all_initialized()
        np.random.seed(hash(op.id) % (2**32))   # 每算子可复现
        env = _Env(hp, ct, np)
        plains = _sample(op, np, n)
        ref = op.ref(np, *plains)
        ciphers = [ct.encrypt(np.asarray(p, dtype=np.float64)) for p in plains]
        cres = op.he(env, *ciphers)
        got = ct.decrypt(cres)
        abs_err, rel_err = _compare(ref, got, np)
        return PResult(op.id, op.kind, op.cost,
                       passed=(abs_err <= op.tol()),
                       max_abs_err=abs_err, max_rel_err=rel_err,
                       secs=round(time.time() - t0, 2))
    except Exception as e:  # noqa: BLE001 —— 对拍要捕获一切,把失败也记成结果
        return PResult(op.id, op.kind, op.cost, passed=False,
                       max_abs_err=float("nan"), max_rel_err=float("nan"),
                       secs=round(time.time() - t0, 2),
                       error=f"{type(e).__name__}: {e}")


def run_all(ids: list[str] | None = None, n: int = 6) -> list[PResult]:
    ops = [by_id(i) for i in ids] if ids else REGISTRY
    ops = [o for o in ops if o is not None]
    return [run_op(o, n) for o in ops]


def _print(results: list[PResult]) -> None:
    print(f"{'算子':<16}{'类别精度':<10}{'代价':<6}{'通过':<5}{'max_abs':<12}{'max_rel':<12}{'秒':<6}备注")
    print("-" * 96)
    ok = 0
    for r in results:
        flag = "✓" if r.passed else "✗"
        if r.passed:
            ok += 1
        ae = "—" if r.max_abs_err != r.max_abs_err else f"{r.max_abs_err:.2e}"
        re_ = "—" if r.max_rel_err != r.max_rel_err else f"{r.max_rel_err:.2e}"
        note = r.error or ""
        print(f"{r.op_id:<16}{r.kind:<10}{r.cost:<6}{flag:<5}{ae:<12}{re_:<12}{r.secs:<6}{note}")
    print("-" * 96)
    print(f"通过 {ok}/{len(results)}")


REPORT_PATH = __import__("pathlib").Path(__file__).resolve().parent / "parity_report.json"


def save_report(results: list[PResult]) -> None:
    import json
    from dataclasses import asdict
    REPORT_PATH.write_text(
        json.dumps({r.op_id: asdict(r) for r in results}, ensure_ascii=False, indent=2),
        encoding="utf-8")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    results = run_all(args or None)
    _print(results)
    if "--save" in sys.argv:
        save_report(results)
        print(f"已写入对拍报告 → {REPORT_PATH.name}")
