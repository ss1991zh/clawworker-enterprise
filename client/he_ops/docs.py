"""
从能力表 + 对拍实测报告生成「算子能力参考」(Markdown)。

这份参考有两个消费者:
  1. 多步规划器(Planner):据此判断每步 HE 能不能做、要不要授权解密、精度够不够;
  2. LLM/skills:让模型只用"实测可靠"的算子,绕开当前构建里坏掉的(greater/less/digitize)。

用法:python -m client.he_ops.docs   →  写出 client/he_ops/OPERATORS.md
"""
from __future__ import annotations

import json
from pathlib import Path

from client.he_ops.registry import REGISTRY

_DIR = Path(__file__).resolve().parent
REPORT = _DIR / "parity_report.json"
OUT = _DIR / "OPERATORS.md"


def _load_report() -> dict:
    try:
        return json.loads(REPORT.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def generate() -> str:
    rep = _load_report()
    verified, unreliable = [], []
    for op in REGISTRY:
        r = rep.get(op.id)
        if r and r.get("passed"):
            verified.append(op.id)
        elif r:
            unreliable.append(op.id)

    lines: list[str] = []
    lines.append("# 密态算子能力参考(对拍实测)")
    lines.append("")
    lines.append("> 由 `registry.py` + `parity_report.json` 自动生成。**只用 ✅ 的算子;⚠ 的当前构建不可靠,规划/codegen 必须绕开。**")
    lines.append("")
    lines.append(f"- ✅ 实测可用({len(verified)}):{', '.join(verified)}")
    if unreliable:
        lines.append(f"- ⚠ 当前不可靠({len(unreliable)}):**{', '.join(unreliable)}** —— 别用,改用等价可靠算子(如用 sort/max/min 取代 greater 比较)。")
    lines.append("")
    lines.append("精度类:exact=仅加减乘(误差≈密文噪声);approx=多项式近似(实测误差见下)。代价=乘法深度粗分级(high 可能很慢)。")
    lines.append("")

    cats = ["arithmetic", "aggregation", "stats", "comparison", "sort", "binning", "math", "linalg"]
    seen = set()
    for cat in cats + sorted({o.category for o in REGISTRY}):
        ops = [o for o in REGISTRY if o.category == cat and o.id not in seen]
        if not ops:
            continue
        seen.update(o.id for o in ops)
        lines.append(f"## {cat}")
        lines.append("")
        lines.append("| 算子 | 状态 | 精度类 | 代价 | 需授权解密 | 实测max误差 | 说明 |")
        lines.append("|---|---|---|---|---|---|---|")
        for o in ops:
            r = rep.get(o.id) or {}
            if r.get("passed"):
                st = "✅"
            elif r:
                st = "⚠ 不可靠"
            else:
                st = "—未测"
            err = r.get("max_abs_err")
            errs = "—" if err is None or err != err else f"{err:.1e}"
            auth = "是" if o.needs_auth_decrypt else "—"
            note = o.note + ((" · " + r["error"]) if r.get("error") else "")
            lines.append(f"| `{o.id}` | {st} | {o.kind} | {o.cost} | {auth} | {errs} | {note} |")
        lines.append("")

    # ---- 表级(pandaseal · CipherDataFrame)----
    try:
        from client.he_ops.parity_df import DF_OPS
        df_rep = json.loads((_DIR / "parity_df_report.json").read_text(encoding="utf-8"))
    except Exception:
        DF_OPS, df_rep = [], {}
    if DF_OPS:
        passed = sum(1 for o in DF_OPS if df_rep.get(o.id, {}).get("passed"))
        lines.append(f"## 表级 · pandaseal(CipherDataFrame,真实分析主用层)")
        lines.append("")
        lines.append(f"对拍实测 {passed}/{len(DF_OPS)} 通过。直接在密文 DataFrame 上 `cdf.xxx()`,解密用 `ct.decrypt_df`。")
        lines.append("")
        lines.append("| 操作 | 状态 | 实测max误差 | 说明 |")
        lines.append("|---|---|---|---|")
        for o in DF_OPS:
            r = df_rep.get(o.id) or {}
            st = "✅" if r.get("passed") else ("⚠" if r else "—")
            err = r.get("max_abs_err")
            errs = "—" if err is None or err != err else f"{err:.1e}"
            lines.append(f"| `{o.id}` | {st} | {errs} | {o.note} |")
        lines.append("")

    def _rep(name):
        try:
            return json.loads((_DIR / name).read_text(encoding="utf-8"))
        except Exception:
            return {}

    # ---- 密态分组聚合(groupby)----
    gb = _rep("parity_groupby_report.json")
    if gb:
        passed = sum(1 for v in gb.values() if v.get("passed"))
        lines.append("## 密态分组聚合 · groupby(明文维度键 × 密文度量)")
        lines.append("")
        lines.append(f"对拍实测 {passed}/{len(gb)} 通过。维度键取自明文身份列;**sum/mean/count 精确**(组大小明文 → 均值=和×1/n),max/min 近似。")
        lines.append("")
        lines.append("| 聚合 | 状态 | 实测max误差 | 沙箱用法 |")
        lines.append("|---|---|---|---|")
        usage = {"sum": "groupby.sum(cdf[度量], keys)", "mean": "groupby.mean(cdf[度量], keys)",
                 "count": "groupby.count(keys)", "max": "groupby.max(cdf[度量], keys)",
                 "min": "groupby.min(cdf[度量], keys)"}
        for k, v in gb.items():
            st = "✅" if v.get("passed") else "⚠"
            err = v.get("max_abs_err"); errs = "—" if err is None or err != err else f"{err:.1e}"
            lines.append(f"| `{k}` | {st} | {errs} | `{usage.get(k, '')}` |")
        lines.append("")

    # ---- 窗口/时序 + 多条件(advanced)----
    adv = _rep("parity_advanced_report.json")
    if adv:
        passed = sum(1 for v in adv.values() if v.get("passed"))
        lines.append("## 窗口/时序 + 多条件 · window / synth(对拍实测)")
        lines.append("")
        lines.append(f"对拍实测 {passed}/{len(adv)} 通过。窗口 `window.*`(diff/lag/rolling 精确,pct_change 近似);")
        lines.append("多条件 `synth.*`(布尔代数 band/bor/bnot 组合掩码 → sumif_and/sumif_or/countif_and/countif_or)。")
        lines.append("")
        lines.append("| 算子 | 状态 | 实测max误差 |")
        lines.append("|---|---|---|")
        for k, v in adv.items():
            st = "✅" if v.get("passed") else "⚠"
            err = v.get("max_abs_err"); errs = "—" if err is None or err != err else f"{err:.1e}"
            lines.append(f"| `{k}` | {st} | {errs} |")
        lines.append("")

    # ---- 模型级(helearn · 体检实测)----
    ml = _rep("parity_ml_report.json")
    lines.append("## 模型级 · helearn(密文训练 + 预测,体检实测)")
    lines.append("")
    if ml:
        lines.append("逐模型对拍明文 sklearn + 留出集真值(回归看 R²,分类看准确率)。")
        lines.append("")
        lines.append("| 模型 | 任务 | 状态 | 密态分 | sklearn | 说明 |")
        lines.append("|---|---|---|---|---|---|")
        for k, v in ml.items():
            st = "✅" if v.get("passed") else "⚠"
            hs = v.get("he_score"); ss = v.get("sk_score")
            hss = "—" if hs is None or hs != hs else f"{hs:.3f}"
            sss = "—" if ss is None or ss != ss else f"{ss:.3f}"
            tail = v.get("error") or v.get("note", "")
            lines.append(f"| `{k}` | {v.get('task','')} | {st} | {hss} | {sss} | {tail} |")
        lines.append("")
    lines.append("- ✅ `LinearRegression`(回归)、`LogisticRegression`(分类,predict 返回 logit,标签=`logit>0`,特征须标准化)。")
    lines.append("- ⚠ `GradientBoosting*` / `XGB*`:当前构建密文训练内部报错(`'tuple' object does not support item assignment`),勿用。")
    lines.append("- `CipherTree` / `XgbCipherTree`:仅 `predict`(推理-only,需预训练树)。")
    lines.append("- 用法:`m.set_params(iterations,w,learningrate)` → `m.fit(X,y)` → `m.predict(X)`;X/y 为密文。")
    lines.append("")

    # ---- 数值护栏(深度剖面)----
    dep = _rep("parity_depth_report.json")
    if dep:
        lines.append("## 数值护栏 · 深度×误差剖面")
        lines.append("")
        lines.append(f"纯乘法链可用深度 ≈ **{dep.get('usable_depth')}**(相对误差预算 {dep.get('budget')});"
                     "超过此深度精度才显著退化。供 planner/verifier 给链式分析预警。")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    OUT.write_text(generate(), encoding="utf-8")
    print(f"已生成算子参考 → {OUT}")
