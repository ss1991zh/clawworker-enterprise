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


def test_web_lookup_routes_to_freechat():
    # 查外部实时信息 → 非分析(即便含"汇总"等聚合词,也走联网/自由聊天)
    assert looks_like_analysis("查一下北京今天天气") is False
    assert looks_like_analysis("汇总一下最新的行业新闻") is False
    assert looks_like_analysis("现在的美元汇率是多少") is False
    # 但若明确针对"这份数据" → 仍是分析,不被实时词带偏
    assert looks_like_analysis("汇总这份数据每个大区的最新回款率") is True


def test_web_lookup_detector():
    from client.webui.pipeline import _looks_like_web_lookup
    assert _looks_like_web_lookup("搜索今日国内热点新闻") is True
    assert _looks_like_web_lookup("查一下上海明天天气") is True
    assert _looks_like_web_lookup("按大区统计回款率") is False
    # 针对"这份数据"的不算外部查询
    assert _looks_like_web_lookup("搜索这份数据里的异常值") is False
