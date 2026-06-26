"""
he_ops 自检 —— 一条命令跑通整套能力层,给统一 PASS/FAIL。

三件用途:
  · 库升级 / 新构建后:回归验证(本来已知坏的之外有没有新坏的)。
  · 用户导入新密钥+字典后:健康自检(这套 key/dict 算子能不能用、精度够不够)。
  · CI:失败返回非 0。

跑:AGENT_BACKEND=real python -m client.he_ops.selfcheck

判定:
  · 数组级:除"当前构建已知坏掉"的 4 个(greater/greater_equal/less/digitize)外全过 → 通过;
    出现新失败 = 回归 = 失败。
  · 表级(pandaseal):全过。
  · 模型级(helearn):LinearRegression 密文训练+预测产出有限值。
  · 规划器:好计划通过校验 + 坏计划被拦(确定性)。
"""
from __future__ import annotations

import sys

# 当前构建实测已知坏掉、且已有 synth 替代,不计为回归
KNOWN_BROKEN = {"greater", "greater_equal", "less", "digitize"}


def _check_array() -> tuple[bool, str]:
    from client.he_ops import parity
    res = parity.run_all()
    fails = [r.op_id for r in res if not r.passed]
    new_fails = [f for f in fails if f not in KNOWN_BROKEN]
    ok = not new_fails
    msg = f"{sum(r.passed for r in res)}/{len(res)} 通过(已知坏:{', '.join(sorted(set(fails) & KNOWN_BROKEN)) or '无'})"
    if new_fails:
        msg += f" · ⚠ 新增失败(回归):{', '.join(new_fails)}"
    return ok, msg


def _check_table() -> tuple[bool, str]:
    from client.he_ops import parity_df
    res = parity_df.run_all()
    fails = [r.op_id for r in res if not r.passed]
    ok = not fails
    return ok, f"{sum(r.passed for r in res)}/{len(res)} 通过" + (f" · ⚠ 失败:{', '.join(fails)}" if fails else "")


def _check_model() -> tuple[bool, str]:
    try:
        import numpy as np
        import crypto_toolkit as ct
        import henumpy as hp
        import helearn as hl
        from client.tools.runtime import Runtime
        Runtime.get().ensure_all_initialized()
        d = hl.datasets.load_diabetes()
        w = hp.ones_array(len(d.feature_names) + 1)
        m = hl.LinearRegression()
        m.set_params(iterations=10, w=w, learningrate=0.1)
        m.fit(d.train_data, d.train_target)
        out = np.ravel(ct.decrypt(m.predict(d.test_data)))
        ok = bool(out.size) and bool(np.all(np.isfinite(out)))
        return ok, f"LinearRegression 预测 {out.size} 条" + ("" if ok else " · ⚠ 含非有限值")
    except Exception as e:  # noqa: BLE001
        return False, f"⚠ {type(e).__name__}: {e}"


def _check_planner() -> tuple[bool, str]:
    from client.he_ops.planner import Plan, Step, validate_plan
    good = Plan([Step("s1", "回款率", ops=["div"]),
                 Step("s2", "条件求和", ops=["sumif_gt"], depends_on=["s1"]),
                 Step("s3", "排名", ops=["sort"], needs_decrypt=True, depends_on=["s2"])])
    bad = Plan([Step("a", "用禁用算子", ops=["greater"], depends_on=["b"]),
                Step("b", "成环", ops=["sum"], depends_on=["a"])])
    vg, vb = validate_plan(good), validate_plan(bad)
    ok = vg.ok and ("s3" in vg.auth_steps) and (not vb.ok)
    return ok, f"好计划{'过' if vg.ok else '未过'}/授权解密标注{'对' if 's3' in vg.auth_steps else '错'}、坏计划{'被拦' if not vb.ok else '漏拦'}"


def main() -> int:
    print("══════════ he_ops 自检 ══════════")
    suites = [
        ("数组级 (henumpy+synth)", _check_array),
        ("表级   (pandaseal)", _check_table),
        ("模型级 (helearn)", _check_model),
        ("规划器 (planner)", _check_planner),
    ]
    all_ok = True
    for name, fn in suites:
        ok, msg = fn()
        all_ok = all_ok and ok
        print(f"  [{'通过' if ok else '失败'}] {name:24} · {msg}")
    print("══════════════════════════════════")
    print("总体:" + ("✅ 全部通过" if all_ok else "❌ 存在失败/回归"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
