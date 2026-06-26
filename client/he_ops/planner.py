"""
多步规划器(Planner)—— Phase 3。

职责:把"复合分析问题"拆成**步骤计划**,且**规划时就受能力表约束**——不规划出
HE 做不了/当前构建坏掉的算子,并标出需要"中途授权解密"的步骤。

本模块分两层:
  · 确定性核心(可单测,不依赖 LLM):
      - capability_brief()  从 registry + 对拍报告自动生成"可靠/禁用算子"摘要,
        供注入 codegen/planner 提示(替代手写,随实测自动保持准确)。
      - validate_plan(plan) 校验计划:禁用算子拦截并给替代、DAG 依赖/环检测、授权解密点标注。
  · LLM 层(需主机代理):
      - build_plan_messages(question, schema)  让 LLM 产出 JSON 步骤计划。
      - parse_plan(text)                        解析为 Plan。

设计取舍:当前"单代码块 + 小样本校验 + 回环"已能扛多数复合问题;Planner 先以
"读能力表 → 产出并校验步骤计划(作为 codegen 的脚手架/护栏)"形态落地,不强行改成
重型 DAG 执行器。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from client.he_ops.registry import REGISTRY, by_id

_DIR = Path(__file__).resolve().parent

# 当前构建实测坏掉的算子 → 可靠替代(由对拍发现)
BROKEN_REPLACEMENT = {
    "greater": "synth.gt", "greater_equal": "synth.ge", "less": "synth.lt",
    "digitize": "synth.bin_index",
}
SYNTH_OPS = {"gt", "lt", "ge", "le", "gt_thr", "between",
             "sumif_gt", "sumif_lt", "countif_gt", "countif_lt", "bin_index"}


def _load_report(name: str) -> dict:
    try:
        return json.loads((_DIR / name).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def reliable_ids() -> set[str]:
    """实测可靠的算子集合(数组级对拍通过 + synth + 表级)。"""
    rep = _load_report("parity_report.json")
    df_rep = _load_report("parity_df_report.json")
    ids = {k for k, v in rep.items() if v.get("passed")}
    ids |= {k for k, v in df_rep.items() if v.get("passed")}
    ids |= SYNTH_OPS
    return ids


def broken_ids() -> set[str]:
    rep = _load_report("parity_report.json")
    return {k for k, v in rep.items() if not v.get("passed")}


def auth_decrypt_ids() -> set[str]:
    """需"中途授权解密"的算子(排序/中位数/分位数/极值等)。"""
    return {op.id for op in REGISTRY if op.needs_auth_decrypt}


def capability_brief() -> str:
    """供注入 LLM 提示的紧凑能力摘要(从实测自动生成)。"""
    rel = reliable_ids()
    broken = broken_ids()
    auth = sorted(auth_decrypt_ids() & rel)
    arr = sorted(o.id for o in REGISTRY if o.id in rel and o.impl == "native")
    syn = sorted(SYNTH_OPS & rel)
    lines = [
        "密态算子可靠性(对拍实测,务必遵守):",
        f"- 数组级 henumpy 可靠:{', '.join(arr)}",
        f"- 合成可靠(比较/条件/分箱):{', '.join(syn)}",
        (f"- ⚠ 当前构建坏掉、禁用:{', '.join(sorted(broken))} → "
         f"改用 {', '.join(f'{k}→{v}' for k, v in BROKEN_REPLACEMENT.items() if k in broken)}"),
        "- 表级 pandaseal(CipherDataFrame)直接可用:列加减乘除、sum/mean/var/std/quantile、sort_values、.gt 等(实测可靠)。",
        f"- 需授权解密(会触发用户授权,规划时标出):{', '.join(auth)}",
        "- 模型级 helearn:LinearRegression 等(密文训练+预测)。",
    ]
    return "\n".join(lines)


# ---------------- 计划模型 ----------------
@dataclass
class Step:
    id: str
    desc: str
    ops: list[str] = field(default_factory=list)
    needs_decrypt: bool = False
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Plan:
    steps: list[Step] = field(default_factory=list)


@dataclass
class Validation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    auth_steps: list[str] = field(default_factory=list)
    order: list[str] = field(default_factory=list)


def parse_plan(text: str) -> Plan:
    """从 LLM 文本里抽 JSON 计划。"""
    m = re.search(r"\{.*\"steps\".*\}", text, re.DOTALL)
    raw = json.loads(m.group(0) if m else text)
    steps = []
    for s in raw.get("steps", []):
        steps.append(Step(
            id=str(s.get("id", "")),
            desc=str(s.get("desc", "")),
            ops=[str(o) for o in (s.get("ops") or [])],
            needs_decrypt=bool(s.get("needs_decrypt", False)),
            depends_on=[str(d) for d in (s.get("depends_on") or [])],
        ))
    return Plan(steps)


def validate_plan(plan: Plan) -> Validation:
    """确定性校验:禁用算子拦截、依赖/环检测、授权解密点标注。"""
    rel, broken, auth = reliable_ids(), broken_ids(), auth_decrypt_ids()
    ids = [s.id for s in plan.steps]
    errors, warnings, auth_steps = [], [], []

    if len(set(ids)) != len(ids):
        errors.append("步骤 id 有重复")

    for s in plan.steps:
        for op in s.ops:
            base = op.split(".")[-1]   # 容忍 "synth.gt" / "hp.sum"
            if base in broken:
                errors.append(f"步骤「{s.id}」用了禁用算子 {base} → 改用 {BROKEN_REPLACEMENT.get(base, 'synth/pandaseal 等价')}")
            elif base in rel or base in SYNTH_OPS:
                pass
            else:
                warnings.append(f"步骤「{s.id}」算子 {base} 不在能力表(可能是 pandas/表级操作,确认可靠)")
        if s.needs_decrypt or any(op.split('.')[-1] in auth for op in s.ops):
            auth_steps.append(s.id)
        for d in s.depends_on:
            if d not in ids:
                errors.append(f"步骤「{s.id}」依赖不存在的步骤「{d}」")

    order = _toposort(plan, errors)
    return Validation(ok=not errors, errors=errors, warnings=warnings,
                      auth_steps=auth_steps, order=order)


def _toposort(plan: Plan, errors: list[str]) -> list[str]:
    deps = {s.id: set(d for d in s.depends_on) for s in plan.steps}
    order, ready = [], [i for i, d in deps.items() if not d]
    deps = {i: set(d) for i, d in deps.items()}
    while ready:
        n = ready.pop(0)
        order.append(n)
        for i, d in deps.items():
            if n in d:
                d.discard(n)
                if not d and i not in order and i not in ready:
                    ready.append(i)
    if len(order) != len(plan.steps):
        errors.append("步骤依赖存在环 / 无法拓扑排序")
    return order


def plan_steps_text(plan: Plan) -> str:
    """渲染给"计算追踪"/用户看。"""
    out = []
    for i, s in enumerate(plan.steps, 1):
        tag = " [需授权解密]" if s.needs_decrypt else ""
        ops = f" · 算子: {', '.join(s.ops)}" if s.ops else ""
        out.append(f"{i}. {s.desc}{ops}{tag}")
    return "\n".join(out)


_PLAN_SYSTEM = """你是密态数据分析的规划器。把用户的复合问题拆成**有序步骤计划**(只规划,不写代码)。
必须遵守下面的算子可靠性约束,不要规划用到"禁用"的算子;涉及排序/分位数/极值等需中途授权解密的步骤,标 needs_decrypt=true。
只输出一个 JSON:{"steps":[{"id":"s1","desc":"中文说明","ops":["用到的算子id"],"needs_decrypt":false,"depends_on":[]}]}
"""


def build_plan_messages(question: str, schema: dict) -> tuple[str, str]:
    system = _PLAN_SYSTEM + "\n" + capability_brief()
    user = (f"数据 schema(只有字段名):\n{json.dumps(schema, ensure_ascii=False)}\n\n"
            f"用户问题:{question}\n\n只输出 JSON 步骤计划。")
    return system, user
