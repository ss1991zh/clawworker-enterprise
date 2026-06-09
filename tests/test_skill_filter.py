"""
固化 skill 实体筛选(filter)回归测试 —— 锁住"点名某产品/大区只算它"的行为。
纯 pandas,不依赖真实 HE。
"""
from __future__ import annotations

import pandas as pd
import pytest

from client.tools.skills import _apply_filter, SKILLS


def _df():
    return pd.DataFrame({
        "产品名称": ["数控伺服驱动器 DR-400", "工业控制器 IPC-100", "数控伺服驱动器 DR-400", "电源模块 PM-200"],
        "销售大区": ["华东大区", "华东大区", "华南大区", "海外"],
        "销量": [10, 20, 15, 5],
    })


def test_no_filter_returns_all():
    df = _df()
    assert len(_apply_filter(df, {})) == 4
    assert len(_apply_filter(df, {"filter": None})) == 4


def test_exact_match():
    out = _apply_filter(_df(), {"filter": {"产品名称": "数控伺服驱动器 DR-400"}})
    assert len(out) == 2
    assert set(out["产品名称"]) == {"数控伺服驱动器 DR-400"}


def test_substring_fallback_when_no_exact():
    # 只说 "DR-400" → 精确无命中 → 子串匹配到全名
    out = _apply_filter(_df(), {"filter": {"产品名称": "DR-400"}})
    assert len(out) == 2
    assert set(out["产品名称"]) == {"数控伺服驱动器 DR-400"}


def test_list_match_is_in():
    out = _apply_filter(_df(), {"filter": {"销售大区": ["华东大区", "海外"]}})
    assert len(out) == 3
    assert "华南大区" not in set(out["销售大区"])


def test_multiple_filters_are_anded():
    out = _apply_filter(_df(), {"filter": {"产品名称": "DR-400", "销售大区": "华东大区"}})
    assert len(out) == 1
    assert out.iloc[0]["销售大区"] == "华东大区"


def test_missing_column_is_skipped_not_error():
    out = _apply_filter(_df(), {"filter": {"不存在的列": "x"}})
    assert len(out) == 4  # 跳过该条件,不致误删


def test_zero_match_raises_with_samples():
    with pytest.raises(ValueError) as ei:
        _apply_filter(_df(), {"filter": {"产品名称": "根本不存在ZZZ"}})
    # 报错信息应含列名 + 样例值,便于用户纠正
    assert "产品名称" in str(ei.value)


def test_all_skills_declare_filter_param():
    """每个固化 skill 的 params 都应声明 filter,LLM 才会填。"""
    missing = [name for name, meta in SKILLS.items() if "filter" not in meta.get("params", [])]
    assert not missing, f"这些 skill 未声明 filter 参数: {missing}"
