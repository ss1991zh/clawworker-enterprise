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
# 当前构建训练内部报错的树模型(非回归),Linear/Logistic 才是达标项
KNOWN_BROKEN_MODELS = {"GradientBoostingRegressor", "GradientBoostingClassifier",
                       "XGBRegressor", "XGBClassfier"}


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


def _check_groupby() -> tuple[bool, str]:
    from client.he_ops import parity_groupby
    res = parity_groupby.run_all()
    fails = [r.agg for r in res if not r.passed]
    return not fails, f"{sum(r.passed for r in res)}/{len(res)} 通过" + (f" · ⚠ {', '.join(fails)}" if fails else "")


def _check_advanced() -> tuple[bool, str]:
    from client.he_ops import parity_advanced
    res = parity_advanced.run_all()
    fails = [r.op_id for r in res if not r.passed]
    return not fails, f"{sum(r.passed for r in res)}/{len(res)} 通过" + (f" · ⚠ {', '.join(fails)}" if fails else "")


def _check_model() -> tuple[bool, str]:
    from client.he_ops import parity_ml
    res = parity_ml.run_all()
    fails = [r.model for r in res if not r.passed]
    new_fails = [f for f in fails if f not in KNOWN_BROKEN_MODELS]
    ok = not new_fails
    passed = [r.model for r in res if r.passed]
    msg = f"达标 {', '.join(passed) or '无'}(树模型已知缺陷:{len(KNOWN_BROKEN_MODELS)})"
    if new_fails:
        msg += f" · ⚠ 新增失败(回归):{', '.join(new_fails)}"
    return ok, msg


def _check_depth() -> tuple[bool, str]:
    from client.he_ops import parity_depth
    prof = parity_depth.run_all()
    d = parity_depth.usable_depth(prof)
    ok = d >= 8
    return ok, f"可用乘法深度 ≈ {d}" + ("" if ok else " · ⚠ 低于 8")


def _check_domain() -> tuple[bool, str]:
    from client.he_ops import parity_domain
    prof = parity_domain.run_all()
    total = sum(len(v["rows"]) for v in prof.values())
    ok_n = sum(1 for v in prof.values() for r in v["rows"] if r["ok"])
    ok = ok_n == total
    return ok, f"近似算子 {ok_n}/{total} 区间可靠" + ("" if ok else " · ⚠ 有区间超差")


def _check_scale() -> tuple[bool, str]:
    from client.he_ops import parity_scale
    r = parity_scale.run_size(200_000)
    ok = r.correct and r.t_groupby < 3.0 and not r.error
    msg = f"20万行 group-by {r.t_groupby:.2f}s · 内存 {r.peak_rss_mb:.0f}MB · 正确{'✓' if r.correct else '✗'}"
    if r.error:
        msg += f" · ⚠ {r.error}"
    elif not ok:
        msg += " · ⚠ 慢于 3s 或不精确"
    return ok, msg


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


# 套件清单(quick=导入体检常用的快子集,跳过较慢的模型/深度/有效域)
_SUITES = [
    ("数组级 (henumpy+synth)", _check_array, True),
    ("表级 (pandaseal)", _check_table, True),
    ("分组 (groupby)", _check_groupby, True),
    ("窗口+多条件 (advanced)", _check_advanced, True),
    ("规模 (scale 20万行)", _check_scale, True),
    ("模型级 (helearn)", _check_model, False),
    ("深度护栏 (depth)", _check_depth, False),
    ("有效域 (domain)", _check_domain, False),
    ("规划器 (planner)", _check_planner, True),
]


def health_report(quick: bool = False) -> dict:
    """结构化体检报告(供 /api/keycheck 导入体检 + CLI 共用)。
    在**当前已加载的用户密钥**上跑对拍套件,返回每套件 ok/detail + 能力摘要 + 规模档位。
    quick=True 跑快子集(跳过模型/深度/有效域),适合导入流程即时反馈。"""
    suites = [(n, f) for n, f, q in _SUITES if (q or not quick)]
    rows, all_ok = [], True
    for name, fn in suites:
        try:
            ok, detail = fn()
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        all_ok = all_ok and ok
        rows.append({"name": name, "ok": ok, "detail": detail})

    brief = scale = ""
    try:
        from client.he_ops.planner import capability_brief
        brief = capability_brief()
    except Exception:  # noqa: BLE001
        pass
    try:
        from client.he_ops import parity_scale
        scale = parity_scale.scale_tier(parity_scale.run_all([200_000]))
    except Exception:  # noqa: BLE001
        scale = {}
    return {"ok": all_ok, "quick": quick, "suites": rows,
            "capability_brief": brief, "scale_tier": scale}


def main() -> int:
    print("══════════ he_ops 自检 ══════════")
    rep = health_report(quick="--quick" in sys.argv)
    for r in rep["suites"]:
        print(f"  [{'通过' if r['ok'] else '失败'}] {r['name']:24} · {r['detail']}")
    print("══════════════════════════════════")
    print("总体:" + ("✅ 全部通过" if rep["ok"] else "❌ 存在失败/回归"))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
