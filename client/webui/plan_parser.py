"""LLM 计算计划响应解析。"""

from __future__ import annotations

import json
import re

from shared.contract import ComputationPlan


PLAN_TAG_RE = re.compile(r"<computation_plan>\s*(\{.*?\})\s*</computation_plan>", re.DOTALL)
PLAN_FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
SUMMARY_TAG_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)


def extract_plan_and_summary(text: str) -> tuple[ComputationPlan, str]:
    if not text or not text.strip():
        raise ValueError("LLM 返回空文本(可能 max_tokens 用光)")
    match = PLAN_TAG_RE.search(text) or PLAN_FENCED_RE.search(text)
    if not match:
        raise ValueError("LLM 响应没找到 <computation_plan> 或 ```json``` 块")
    try:
        plan_dict = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"computation_plan 不是合法 JSON:{exc}") from exc
    plan = ComputationPlan.model_validate(plan_dict)
    summary_match = SUMMARY_TAG_RE.search(text)
    summary = summary_match.group(1).strip() if summary_match else ""
    if not summary:
        after = text.split("</computation_plan>", 1)
        summary = after[1].strip()[:500] if len(after) == 2 else ""
    return plan, summary or "已生成分析,详见 Excel。"
