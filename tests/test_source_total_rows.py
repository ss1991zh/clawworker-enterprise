"""
源数据合计行剔除 + 结果级偷懒验收 回归测试。
"""
from __future__ import annotations

import pandas as pd

from client.tools.skills import drop_source_total_rows
from client.webui.pipeline import _results_look_truncated


def _df_with_total(label="合计 / 平均"):
    rows = [{"序号": str(i), "销售代表": f"人{i}", "大区": "华东", "金额": float(i)} for i in range(1, 6)]
    rows.append({"序号": label, "销售代表": None, "大区": None, "金额": 15.0})
    return pd.DataFrame(rows)


def test_keyword_total_row_dropped():
    out, n = drop_source_total_rows(_df_with_total("合计 / 平均"))
    assert n == 1 and len(out) == 5
    assert "合计 / 平均" not in set(out["序号"])


def test_empty_id_total_row_dropped():
    # 身份列几乎全空(连"合计"字样都没有)也能识别
    out, n = drop_source_total_rows(_df_with_total(label=None))
    assert n == 1 and len(out) == 5


def test_normal_rows_kept():
    df = pd.DataFrame({"序号": ["1", "2"], "销售代表": ["甲", "乙"], "大区": ["A", "B"], "金额": [1.0, 2.0]})
    out, n = drop_source_total_rows(df)
    assert n == 0 and len(out) == 2


def test_middle_sparse_row_not_dropped():
    # 中间的数据质量差的行不在表尾连续区段 → 不剔
    df = pd.DataFrame({
        "序号": ["1", None, "3"], "销售代表": ["甲", None, "丙"],
        "大区": ["A", None, "C"], "金额": [1.0, 2.0, 3.0],
    })
    out, n = drop_source_total_rows(df)
    assert n == 0 and len(out) == 3


# 模拟身份列:18 个销售代表 × 多月 ≈ 100 行
_META = [{"销售代表": f"代表{i % 18}", "销售大区": f"区{i % 6}"} for i in range(100)]


def test_results_truncation_detected():
    small = [{"df": pd.DataFrame({"x": range(10)})}]   # 数据 101 行只出 10 行(≠任何实体数)
    assert _results_look_truncated(small, 101, "这100个人的边际贡献率", _META) == 10


def test_results_full_passes():
    full = [{"df": pd.DataFrame({"x": range(100)})}]
    assert _results_look_truncated(full, 101, "这100个人的边际贡献率", _META) == 0


def test_any_aggregation_without_detail_is_flagged():
    """每行都是独立记录,永不合并:任何聚合压行(没附全量明细)都算截断。"""
    by_person = [{"df": pd.DataFrame({"x": range(18)})}]
    assert _results_look_truncated(by_person, 101, "所有人的回款率", _META) == 18
    # 点名维度也一样:"按大区"是排序口径,不是压成 6 行
    by_region = [{"df": pd.DataFrame({"x": range(6)})}]
    assert _results_look_truncated(by_region, 101, "所有大区的回款率", _META) == 6
    by_person2 = [{"df": pd.DataFrame({"x": range(18)})}]
    assert _results_look_truncated(by_person2, 101, "所有销售代表的回款率汇总", _META) == 18


def test_aggregation_with_full_detail_passes():
    """聚合 sheet 可以有,但必须同时有行数≈数据行数的逐行明细。"""
    mixed = [{"df": pd.DataFrame({"x": range(6)})},
             {"df": pd.DataFrame({"x": range(100)})}]
    assert _results_look_truncated(mixed, 101, "所有大区的回款率", _META) == 0


def test_ninety_percent_threshold():
    """明细行数 ≥ 数据的 90%(容许剔合计/坏行)即通过;明显缩水仍抓。"""
    ok = [{"df": pd.DataFrame({"x": range(95)})}]
    assert _results_look_truncated(ok, 101, "所有人的回款率", _META) == 0
    shrunk = [{"df": pd.DataFrame({"x": range(60)})}]
    assert _results_look_truncated(shrunk, 101, "所有人的回款率", _META) == 60


def test_filter_query_subset_is_legal():
    """筛选类问题(找异常/超期)合法输出子集,不验收全量。"""
    few = [{"df": pd.DataFrame({"x": range(8)})}]
    assert _results_look_truncated(few, 101, "找出回款率异常的明细", _META) == 0
    assert _results_look_truncated(few, 101, "列出所有逾期超过90天的记录", _META) == 0


def test_results_agg_plus_detail_passes():
    mixed = [{"df": pd.DataFrame({"x": range(6)})},      # 聚合 6 行
             {"df": pd.DataFrame({"x": range(100)})}]    # 全量明细
    assert _results_look_truncated(mixed, 101, "所有人的回款率", _META) == 0


def test_results_check_skips_topn_and_small_data():
    small = [{"df": pd.DataFrame({"x": range(10)})}]
    assert _results_look_truncated(small, 101, "回款率前10名", _META) == 0   # TOP-N 合法
    assert _results_look_truncated(small, 20, "所有人的回款率", _META) == 0  # 小数据不查
