"""LLM 偷懒截断检测回归测试。"""
from client.webui.pipeline import _detect_lazy_truncation as lazy


def test_full_query_with_head_is_lazy():
    assert lazy("计算这100个人的边际贡献率", "df = ct.decrypt_df(cdf).head(10)")
    assert lazy("所有人的回款率", "x = full.sample(20)")
    assert lazy("每位销售代表的明细", "full = full.iloc[:15]")


def test_topn_query_is_not_lazy():
    assert not lazy("回款率最高的前10名", "full.nlargest(10, '回款率')")
    assert not lazy("TOP5 销售额", "full.head(5)")


def test_no_truncation_is_not_lazy():
    assert not lazy("所有人的回款率", "full = ct.decrypt_df(cdf)\ng = full.groupby('大区').mean()")


def test_vague_query_without_full_hint_is_not_lazy():
    assert not lazy("回款率", "full.head(10)")
