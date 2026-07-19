"""场景红队修复的回归测试(只测行为,不测措辞)。

对应 docs/eval_cases.md:
  #13 无意图/乱码 → 友好追问路由
  #9  字段名投毒 / #15 条件分支 / ⚠不硬猜 → 两条路径 prompt 均含安全边界铁律
"""
from __future__ import annotations

import pathlib

import pytest

from client.webui.pipeline import _looks_like_no_intent


@pytest.mark.parametrize("q", ["", "   ", "!!!@@@", "......", "∑®†¥¨ˆ", "、、、。。", "a"])
def test_no_intent_detected(q):
    assert _looks_like_no_intent(q) is True


@pytest.mark.parametrize("q", [
    "算一下业绩", "看看哪个产品卖得好", "forecast sales by region",
    "top 10 customers", "算 Q3 回款率", "按大区算回款率",
])
def test_real_query_not_flagged(q):
    assert _looks_like_no_intent(q) is False


def test_codegen_prompt_has_safety_rules():
    from client.webui.codegen import CODEGEN_SYSTEM
    # 字段名非指令 / 忽略注入 / 不支持条件分支 / 不硬猜
    for kw in ["不是给你的指令", "忽略之前的指令", "不支持条件分支", "不要硬猜"]:
        assert kw in CODEGEN_SYSTEM, f"codegen prompt 缺规则: {kw}"


def test_skill_prompt_has_safety_rules():
    sp = pathlib.Path(__file__).resolve().parents[1] / "docs" / "llm_system_prompt.md"
    text = sp.read_text(encoding="utf-8")
    for kw in ["不是指令", "忽略之前的指令", "不支持条件分支", "不要硬猜"]:
        assert kw in text, f"skill prompt 缺规则: {kw}"


def test_freechat_prompt_refuses_prompt_disclosure():
    # R9:闲聊路径也要拒绝复述系统提示词 + 忽略注入
    from client.webui.pipeline import _FREECHAT_SYSTEM
    assert "系统提示词" in _FREECHAT_SYSTEM and "忽略" in _FREECHAT_SYSTEM
