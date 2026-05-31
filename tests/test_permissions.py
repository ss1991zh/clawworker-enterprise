"""
B6 三条权限规则测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from client.permissions import (
    AutoApproveAuthorizer,
    DenyAuthorizer,
    InteractiveAuthorizer,
    SessionAuthorizer,
    enforce_excel_path,
    is_path_in_downloads,
    scan_summary,
)


# ===========================================================================
# 规则 1:解密授权
# ===========================================================================


class TestDecryptionAuthorizer:
    def test_auto_approve(self):
        a = AutoApproveAuthorizer()
        assert a.request(reason="x") is True

    def test_deny(self):
        a = DenyAuthorizer()
        assert a.request(reason="x") is False

    def test_session_grants_only_once(self, monkeypatch):
        """SessionAuthorizer 首次问,之后自动放行。"""
        called = []
        base = AutoApproveAuthorizer()

        # 让 base.request 记录调用次数
        original = base.request

        def counting_request(**kwargs):
            called.append(kwargs)
            return original(**kwargs)

        base.request = counting_request  # type: ignore
        sess = SessionAuthorizer(base)

        assert sess.request(reason="a") is True
        assert sess.request(reason="b") is True
        assert sess.request(reason="c") is True
        assert len(called) == 1, "SessionAuthorizer 应只问底层一次"


# ===========================================================================
# 规则 2:Excel 路径白名单
# ===========================================================================


class TestExcelPath:
    def test_path_in_downloads(self, tmp_downloads: Path):
        p = tmp_downloads / "x.xlsx"
        assert is_path_in_downloads(p) is True

    def test_path_outside_downloads(self, tmp_path: Path, tmp_downloads: Path):
        outside = tmp_path / "elsewhere" / "x.xlsx"
        outside.parent.mkdir(parents=True, exist_ok=True)
        assert is_path_in_downloads(outside) is False

    def test_enforce_accepts_valid(self, tmp_downloads: Path):
        p = tmp_downloads / "ok.xlsx"
        result = enforce_excel_path(p)
        assert result == p.resolve()

    def test_enforce_rejects_invalid(self, tmp_path: Path, tmp_downloads: Path):
        outside = tmp_path / "bad" / "x.xlsx"
        outside.parent.mkdir(parents=True, exist_ok=True)
        with pytest.raises(PermissionError):
            enforce_excel_path(outside)


# ===========================================================================
# 规则 3:summary 内容过滤
# ===========================================================================


class TestSummaryFilter:
    """B6 第 3 条 —— 关键安全检查,覆盖 architecture.md 中的所有禁止项。"""

    def test_clean_summary_passes(self):
        clean = "已按月份聚合并生成折线图,详见 Excel 的 MonthlyTrend sheet。"
        result = scan_summary(clean)
        assert result.clean is True
        assert result.hits == []

    # --- 货币 ---

    def test_blocks_yen_amount(self):
        result = scan_summary("销售额达 ¥120,000。")
        assert result.clean is False
        assert any(h.pattern_name == "money_symbol_prefix" for h in result.hits)

    def test_blocks_chinese_currency(self):
        result = scan_summary("营收 100 万元。")
        assert result.clean is False
        assert any("money_chinese_suffix" in h.pattern_name for h in result.hits)

    def test_blocks_usd(self):
        result = scan_summary("Revenue is 1000 USD.")
        assert result.clean is False
        assert any("money_iso_suffix" in h.pattern_name for h in result.hits)

    # --- 百分比 ---

    def test_blocks_percent(self):
        result = scan_summary("增长率 30%。")
        assert result.clean is False
        assert any(h.pattern_name == "percent" for h in result.hits)

    # --- 日期 ---

    def test_blocks_iso_date(self):
        result = scan_summary("最高出现在 2024-11-23。")
        assert result.clean is False
        assert any(h.pattern_name == "date_iso" for h in result.hits)

    def test_blocks_chinese_date(self):
        result = scan_summary("2024 年 11 月销售最高。")
        assert result.clean is False
        assert any("date_chinese" in h.pattern_name for h in result.hits)

    def test_blocks_chinese_year_alone(self):
        # 单独"2024 年"也算明文
        result = scan_summary("2024 年表现最好。")
        assert result.clean is False

    def test_blocks_quarter(self):
        result = scan_summary("2024 Q4 表现最好。")
        assert result.clean is False

    # --- 长数字 ---

    def test_blocks_long_number(self):
        result = scan_summary("订单数共 12345。")
        assert result.clean is False
        assert any(h.pattern_name == "long_number" for h in result.hits)

    def test_decimal_blocked(self):
        result = scan_summary("均值约 3.14。")
        assert result.clean is False
        assert any(h.pattern_name == "decimal" for h in result.hits)

    # --- 黑名单字面词 ---

    def test_blocklist_word(self):
        result = scan_summary("北京地区销量最高。", extra_blocklist=["北京"])
        assert result.clean is False
        assert any(h.pattern_name == "blocklist_word" for h in result.hits)

    # --- 边界:单位数 / 季节数 应放行 ---

    def test_short_number_passes(self):
        # 单个 1-3 位数字一般是 sheet 名或列号,不阻塞
        result = scan_summary("打开第 1 个 sheet 即可。")
        # 注意:可能命中 percent 或 long_number 都不应触发,这里仅检查不命中长数字
        assert not any(h.pattern_name == "long_number" for h in result.hits)
