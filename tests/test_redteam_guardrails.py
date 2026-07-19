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


# ── 优化循环 T0-1(财务场景评审)修复:预算差异档位色 + 方向歧义率不误染 ──

def test_budget_variance_tier_colors():
    from client.webui.writer import _tier_fill, _TIER_GOOD, _TIER_BAD
    def bg(v):
        r = _tier_fill(v)
        return r[0].fgColor.rgb[-6:] if r else None
    # 收入方向:超额/达标=好(绿),未达=坏(红);成本方向 超支=红/节约=绿
    assert bg("超额") == _TIER_GOOD[0] and bg("达标") == _TIER_GOOD[0]
    assert bg("未达") == _TIER_BAD[0] and bg("超支") == _TIER_BAD[0]
    assert bg("节约") == _TIER_GOOD[0]


def test_ambiguous_direction_rate_not_reverse_colored():
    from client.webui.writer import _AMBIGUOUS_DIRECTION, _REVERSE_METRIC
    # 差异率方向取决于成本/收入,不套逆向色阶(交给档位列),避免把收入超额染红
    assert "差异率" in _AMBIGUOUS_DIRECTION and "差异率" not in _REVERSE_METRIC


# ── 优化循环 T0-1b(财务同比环比评审)锁口径:增长额=金额、增长率=% ──

def test_yoy_mom_amount_vs_rate_format():
    from client.webui.writer import _infer_number_format, _MONEY_FMT
    # 增长额/差额是绝对金额 → 金额格式(红括号);增长率/率是百分比 → 0.00%
    for h in ("环比增长额", "同比增长额", "环比增长", "同比增长", "差异额"):
        assert _infer_number_format(h) == _MONEY_FMT, f"{h} 应为金额格式"
    for h in ("环比增长率", "同比增长率", "环比率", "同比率", "差异率"):
        assert _infer_number_format(h) == "0.00%", f"{h} 应为百分比"


# ── 优化循环 T0-2(库存场景评审)修复:ABC剔负值不溢出 + 周转数据异常隔离 ──

def _inv_frames():
    import pandas as pd
    from client.tools import skills
    skills._decrypt = lambda cdf: cdf.copy()
    rows = [
        ("正常件A","IT", 55000, 400000),   # DIO~50 正常
        ("慢件B","办公", 96000, 240000),   # DIO~146 呆滞
        ("零动C","礼品", 20000, 0),        # 零销货成本 → 呆滞(NaN)
        ("退货冲减","耗材", -15000, 90000), # 负库存 → 数据异常
        ("退供件","耗材", 60000, -20000),  # 负销货成本 → 数据异常
    ]
    df = pd.DataFrame(rows, columns=["物料名称","类别","库存金额","销货成本"])
    meta = df[["物料名称","类别"]].to_dict("records")
    cdf = df[["库存金额","销货成本"]].reset_index(drop=True)
    return skills, cdf, meta


def test_pareto_abc_excludes_nonpositive_no_overflow():
    skills, cdf, meta = _inv_frames()
    _, d, _ = skills.run_skill("pareto_abc", cdf,
        {"label_col":"物料名称","value_col":"库存金额"}, meta, ["物料名称","类别"])
    pos = d[d["ABC类"] != "数据异常"]
    # 正值累计占比不得突破 100%
    assert pos["累计占比"].max() <= 1.0 + 1e-9
    # 负值行被隔离为「数据异常」,占比/累计留空,不混入 ABC
    bad = d[d["ABC类"] == "数据异常"]
    assert len(bad) == 1 and bad["占比"].isna().all() and bad["累计占比"].isna().all()


def test_inventory_turnover_anomaly_blank_and_bottom():
    import numpy as np
    skills, cdf, meta = _inv_frames()
    _, d, _ = skills.run_skill("inventory_turnover", cdf,
        {"item_col":"物料名称","stock_col":"库存金额","cogs_col":"销货成本","days":365},
        meta, ["物料名称","类别"])
    # 数据异常行:周转天数置空(不显示无意义负数)
    bad = d[d["库存状态"] == "数据异常"]
    assert len(bad) == 2 and bad["周转天数"].isna().all()
    # 数据异常置于表尾
    assert (d["库存状态"] == "数据异常").iloc[-len(bad):].all()
    # 零销货成本的呆滞(NaN=不周转)排在最前
    assert d.iloc[0]["库存状态"] == "呆滞" and np.isnan(d.iloc[0]["周转天数"])
