"""
意图识别回归测试 —— 知识/概念提问 vs 对数据做密态分析。
"""
from __future__ import annotations

import pytest

from client.webui.pipeline import looks_like_analysis, _looks_like_knowledge_question


@pytest.mark.parametrize("q", [
    "RFM 的标准计算口径是什么",
    "目标完成率怎么算",
    "什么是边际贡献率",
    "回款率的计算公式是什么",
    "ABC 分析的原理",
    "RFM 适用于什么场景",
    "同比和环比有什么区别",
    "介绍一下帕累托分析",
])
def test_knowledge_questions_route_to_freechat(q):
    assert _looks_like_knowledge_question(q) is True
    assert looks_like_analysis(q) is False, f"知识问题不应判为分析: {q}"


@pytest.mark.parametrize("q", [
    "计算这100个人的RFM分群",
    "按大区统计回款率",
    "这份数据的目标完成率怎么算",     # 含「这份」数据操作标记 → 仍是分析
    "每位销售代表的边际贡献率",
    "TOP10 销售额",
    "导出各产品线的销量汇总",
])
def test_data_ops_route_to_analysis(q):
    assert looks_like_analysis(q) is True, f"对数据的操作应判为分析: {q}"


def test_plain_chat_not_analysis():
    assert looks_like_analysis("你好,你能做什么") is False
    assert looks_like_analysis("今天天气不错") is False
