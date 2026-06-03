"""
Plan 校验 / 自动修复层 —— 兜底各家 LLM 的输出偏差。

典型场景:
  · LLM 把 sheet 取名「100 人回款率」+ value_cols 只放原始两列,**没加 compute**
  · LLM 把 sort_by 写成"回款率"但 compute 里不存在
  · LLM 用 ratio_by_group 但忘了填 num_col / den_col
  · LLM 把指标名拼错(回款率 → 回款比率 等同义词)

这一层在 pipeline.ask 拿到 plan 之后、跑 skill 之前先扫一遍,
能补就补,不能补就发警告到 trace,但不阻断 plan 跑。
"""

from __future__ import annotations

import re
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# 派生指标识别 —— 用户问"X率/X比例/X差/X贡献"必须 compute 派生
# ---------------------------------------------------------------------------

# (识别关键词, 业务公式 builder)
# builder 签名:builder(schema_cols: list[str]) -> Optional[dict]
#   返回 row_detail.compute 单条 dict,如:
#     {"name":"回款率","op":"div","operands":["回款金额(元)","实际销售额(元)"]}
#   字段名能从 schema_cols 推导出来就返回,推不出来返回 None。

def _find_col(schema_cols: list[str], *keyword_groups: list[str]) -> Optional[str]:
    """
    在 schema 列名里找出第一个**全部命中**任一关键词组的列。
      _find_col(cols, ["回款"], ["回款", "金额"])
        → 优先匹配"回款金额(元)"(命中第二组的两个关键词)
    """
    # 优先用关键词多的(更精确的)组先匹配
    sorted_groups = sorted(keyword_groups, key=len, reverse=True)
    for group in sorted_groups:
        for col in schema_cols:
            if all(k in col for k in group):
                return col
    return None


def _ratio(name: str, schema_cols: list[str],
           num_groups: list[list[str]], den_groups: list[list[str]]) -> Optional[dict]:
    num = _find_col(schema_cols, *num_groups)
    den = _find_col(schema_cols, *den_groups)
    if num and den:
        return {"name": name, "op": "div", "operands": [num, den]}
    return None


def _gross_margin(name: str, schema_cols: list[str]) -> Optional[dict]:
    """毛利 / 边际贡献 = 销售收入 − 变动成本"""
    rev = _find_col(schema_cols, ["销售", "收入"], ["实际", "销售"], ["销售额"])
    cost = _find_col(schema_cols, ["变动", "成本"], ["成本"])
    if rev and cost:
        return {"name": name, "op": "sub", "operands": [rev, cost]}
    return None


def _gross_margin_rate(name: str, schema_cols: list[str]) -> Optional[dict]:
    """毛利率 / 边际贡献率 = (收入 − 成本) / 收入,要分两步派生 → 返回特殊 marker"""
    rev = _find_col(schema_cols, ["销售", "收入"], ["实际", "销售"], ["销售额"])
    cost = _find_col(schema_cols, ["变动", "成本"], ["成本"])
    if rev and cost:
        # 两步:先派生中间列再除
        return {
            "_two_step": True,
            "intermediate": {"name": "_毛利暂存", "op": "sub", "operands": [rev, cost]},
            "final": {"name": name, "op": "div", "operands": ["_毛利暂存", rev]},
        }
    return None


# 关键词 → builder 映射(关键词按用户口语化习惯)
# 注意:更长 / 更具体的指标放前面(贪婪匹配)
DERIVED_RULES: list[tuple[list[str], Callable[[str, list[str]], Optional[dict]]]] = [
    # 长名字优先
    (["回款率", "回款比率", "回款比例"],
     lambda nm, cols: _ratio(nm, cols, [["回款"], ["回款", "金额"]], [["实际", "销售"], ["销售额"]])),
    (["目标完成率", "完成率", "达成率"],
     lambda nm, cols: _ratio(nm, cols, [["实际", "销售"], ["销售额"]], [["月度", "目标"], ["目标"]])),
    (["毛利率"],
     lambda nm, cols: _gross_margin_rate(nm, cols)),
    (["毛利"],
     lambda nm, cols: _gross_margin(nm, cols)),
    (["边际贡献率"],
     lambda nm, cols: _gross_margin_rate(nm, cols)),
    (["边际贡献"],
     lambda nm, cols: _gross_margin(nm, cols)),
    # 应发提成 / 销售提成 没有标准比例,无法自动补
]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def _extract_mentioned_metrics(text: str) -> list[str]:
    """从一段文本里抠出可能的派生指标名。
    Pass 1:权威匹配 —— DERIVED_RULES 里硬编码的标准指标名(回款率 / 毛利率 / ...)
    Pass 2:正则兜底 —— "X率 / X比例" 但要满足:
              · 不在 auth 里
              · 不**包含**任何 auth 词作为子串(避免"人回款率"覆盖"回款率")
    """
    if not text:
        return []

    # Pass 1
    auth: list[str] = []
    for keywords, _ in DERIVED_RULES:
        for kw in keywords:
            if kw in text and kw not in auth:
                auth.append(kw)
    # 权威表内部 dedup:短词被长词吃(毛利 ⊂ 毛利率 → 留毛利率)
    auth = [a for a in auth if not any(a != b and a in b for b in auth)]

    # Pass 2
    extras: list[str] = []
    for m in re.finditer(r"([一-龥]{1,5})(?:率|比例|占比|差额|贡献|利润)", text):
        word = m.group(0)
        if word in auth or word in extras:
            continue
        # 含权威词的子串 → 这是 regex 噪声(如"人回款率"含"回款率")
        if any(a in word for a in auth):
            continue
        extras.append(word)

    return auth + extras


def _builder_for(metric_name: str):
    """找该指标名对应的 formula builder。"""
    for keywords, builder in DERIVED_RULES:
        for kw in keywords:
            if kw in metric_name:
                return builder
    return None


def validate_and_repair_plan(plan, schema: dict, log_fn=None) -> tuple[Any, list[str]]:
    """
    扫 plan,自动修复常见的派生列缺失。
    返回 (修复后的 plan, list[warnings])。
    warnings 里:
      "fixed: ..."  → 我们自动补了
      "warn: ..."   → 检测到但无法补,需要 LLM 重试 / 字段补全
    """
    warnings: list[str] = []
    if not plan or not getattr(plan, "skill_calls", None):
        return plan, warnings

    # 取 schema 字段名
    schema_cols: list[str] = []
    for f in (schema.get("fields") or []):
        nm = f.get("name") if isinstance(f, dict) else None
        if nm:
            schema_cols.append(nm)
    for f in (schema.get("columns") or []):
        nm = f.get("name") if isinstance(f, dict) else None
        if nm and nm not in schema_cols:
            schema_cols.append(nm)

    for i, sc in enumerate(plan.skill_calls, 1):
        warnings.extend(_validate_one(sc, schema_cols, i))

    if log_fn and warnings:
        for w in warnings:
            kind = "result" if w.startswith("fixed:") else "error"
            log_fn(kind, w)

    return plan, warnings


def _validate_one(sc, schema_cols: list[str], idx: int) -> list[str]:
    """对一个 SkillCall 跑校验。"""
    warnings: list[str] = []
    params = sc.params or {}

    # ─── ratio_by_group / row_ratio_then_group_mean:必须有 num_col + den_col ───
    if sc.skill in ("ratio_by_group", "row_ratio_then_group_mean"):
        num_col = params.get("num_col") or params.get("num")
        den_col = params.get("den_col") or params.get("den")

        # 自动补:从 metric_name / sheet_name 反查 DERIVED_RULES
        if (not num_col or not den_col):
            hint = " ".join(filter(None, [
                str(params.get("metric_name") or ""),
                str(sc.sheet_name or ""),
            ]))
            mentioned = _extract_mentioned_metrics(hint)
            for m in mentioned:
                builder = _builder_for(m)
                if not builder:
                    continue
                formula = builder(m, schema_cols)
                if not formula or formula.get("_two_step"):
                    continue
                # formula = {name, op, operands: [num, den]}
                if formula.get("op") == "div" and len(formula.get("operands", [])) == 2:
                    if not num_col or num_col not in schema_cols:
                        num_col = formula["operands"][0]
                        params["num_col"] = num_col
                    if not den_col or den_col not in schema_cols:
                        den_col = formula["operands"][1]
                        params["den_col"] = den_col
                    warnings.append(
                        f"fixed: skill_calls[{idx}].{sc.skill} 按「{m}」自动补 "
                        f"num_col={num_col} / den_col={den_col}"
                    )
                    break

        # 仍然不行 → warn
        if not num_col or num_col not in schema_cols:
            warnings.append(
                f"warn: skill_calls[{idx}].{sc.skill} 缺有效 num_col(收到 {num_col!r})"
            )
        if not den_col or den_col not in schema_cols:
            warnings.append(
                f"warn: skill_calls[{idx}].{sc.skill} 缺有效 den_col(收到 {den_col!r})"
            )
        sc.params = params
        return warnings

    # ─── row_detail / top_n_by / group_stats:扫派生指标 ───
    if sc.skill not in ("row_detail", "top_n_by", "group_stats"):
        return warnings

    # 收集文本(sheet_name + value_cols + sort_by)用来抠"提到的派生指标"
    text_bag = " ".join(filter(None, [
        sc.sheet_name or "",
        " ".join(params.get("value_cols") or []),
        " ".join(params.get("value_col") or []) if isinstance(params.get("value_col"), list) else str(params.get("value_col") or ""),
        str(params.get("sort_by") or ""),
    ]))
    mentioned = _extract_mentioned_metrics(text_bag)
    if not mentioned:
        return warnings

    # 已经有的派生 / schema 已有的列
    existing_derived = set()
    for c in (params.get("compute") or []):
        if isinstance(c, dict) and c.get("name"):
            existing_derived.add(c["name"])

    missing: list[str] = []
    for m in mentioned:
        if m in existing_derived or m in schema_cols:
            continue
        missing.append(m)
    if not missing:
        return warnings

    # 试着自动补
    if sc.skill != "row_detail":
        # group_stats / top_n_by 不支持 compute → 只能 warn
        warnings.append(
            f"warn: skill_calls[{idx}].{sc.skill} 提到派生指标 {missing} "
            f"但该 skill 不支持 compute · 建议用 row_detail 或 ratio_by_group"
        )
        return warnings

    if "compute" not in params or params["compute"] is None:
        params["compute"] = []

    for m in missing:
        builder = _builder_for(m)
        if not builder:
            warnings.append(
                f"warn: skill_calls[{idx}].row_detail 检测到「{m}」"
                f"但无内置公式 · 请用户在附件里写明计算方式或显式给 compute"
            )
            continue
        formula = builder(m, schema_cols)
        if not formula:
            warnings.append(
                f"warn: 想补「{m}」但 schema 缺关键字段 · "
                f"可用字段: {schema_cols[:8]}"
            )
            continue
        # 处理两步式(毛利率 = (收入-成本)/收入)
        if isinstance(formula, dict) and formula.get("_two_step"):
            params["compute"].append(formula["intermediate"])
            params["compute"].append(formula["final"])
            warnings.append(
                f"fixed: 自动补派生「{m}」"
                f"(两步:{formula['intermediate']['name']} = "
                f"{formula['intermediate']['operands']}; "
                f"{formula['final']['name']} = {formula['final']['operands']})"
            )
        else:
            params["compute"].append(formula)
            warnings.append(
                f"fixed: 自动补派生「{m}」= {formula['op']}({formula['operands']})"
            )
    sc.params = params
    return warnings
