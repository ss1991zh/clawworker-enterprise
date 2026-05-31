"""
B6 基础权限约束 —— 执行层的三条硬规则(architecture.md §B6)。

| # | 规则 | 触发点 |
|---|------|--------|
| 1 | 解密操作必须经用户授权 | 调用 zfhe 解密前 |
| 2 | Excel 写入路径白名单,只允许 ~/Downloads/ | 写文件前 |
| 3 | LLM 回答内容过滤:禁止具体数值/日期/名称/样本 | summary 展示前 |

第 3 条尤其重要:即使 LLM 偶尔越界,本模块的扫描也会拦下并要求重生成。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


# ===========================================================================
# 规则 1:解密授权
# ===========================================================================


class DecryptionAuthorizer:
    """
    解密授权钩子。

    生产环境实现:弹窗 / OS keychain 二次认证 / OAuth callback 等。
    测试环境实现:auto_approve / never_approve / record_request。

    MVP 提供两种内置实现:
    - InteractiveAuthorizer (CLI y/n 提示)
    - SessionAuthorizer (一次授权,会话内复用)
    """

    def request(self, *, reason: str, data_hint: str = "") -> bool:
        """请求用户授权解密。返回 True 表示放行。"""
        raise NotImplementedError


class InteractiveAuthorizer(DecryptionAuthorizer):
    """命令行交互式授权 —— 适合 CLI 客户端。"""

    def request(self, *, reason: str, data_hint: str = "") -> bool:
        prompt = f"\n[解密授权请求] 原因:{reason}"
        if data_hint:
            prompt += f"  数据:{data_hint}"
        prompt += "\n是否允许解密? [y/N]: "
        try:
            ans = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        return ans in ("y", "yes")


class SessionAuthorizer(DecryptionAuthorizer):
    """
    会话级一次授权:第一次问用户,之后会话内自动放行。
    """

    def __init__(self, underlying: DecryptionAuthorizer):
        self._underlying = underlying
        self._granted = False

    def request(self, *, reason: str, data_hint: str = "") -> bool:
        if self._granted:
            return True
        ok = self._underlying.request(reason=reason, data_hint=data_hint)
        if ok:
            self._granted = True
        return ok


class AutoApproveAuthorizer(DecryptionAuthorizer):
    """测试用:总是放行。生产环境严禁使用。"""

    def request(self, *, reason: str, data_hint: str = "") -> bool:
        return True


class DenyAuthorizer(DecryptionAuthorizer):
    """测试用:总是拒绝,验证拦截路径。"""

    def request(self, *, reason: str, data_hint: str = "") -> bool:
        return False


# ===========================================================================
# 规则 2:Excel 写入路径白名单
# ===========================================================================


DOWNLOADS_DIR = Path.home() / "Downloads"


def is_path_in_downloads(path: str | Path) -> bool:
    """检查路径是否在 ~/Downloads/ 之内(B6 第 2 条)。"""
    p = Path(path).expanduser().resolve()
    try:
        p.relative_to(DOWNLOADS_DIR.resolve())
        return True
    except ValueError:
        return False


def enforce_excel_path(path: str | Path) -> Path:
    """
    强制校验:Excel 写入路径必须在 ~/Downloads/。
    通过则返回规范化后的 Path,不通过抛 PermissionError。
    """
    if not is_path_in_downloads(path):
        raise PermissionError(
            f"[B6-2] Excel 路径不在 ~/Downloads/ 白名单内: {path}"
        )
    return Path(path).expanduser().resolve()


# ===========================================================================
# 规则 3:LLM 回答内容过滤(零明文)
# ===========================================================================


@dataclass
class FilterHit:
    """一次正则命中。"""

    pattern_name: str
    matched_text: str
    start: int
    end: int


@dataclass
class FilterResult:
    """summary 扫描结果。"""

    clean: bool
    hits: list[FilterHit] = field(default_factory=list)

    def report(self) -> str:
        if self.clean:
            return "无命中"
        return "; ".join(
            f"[{h.pattern_name}] '{h.matched_text}'@({h.start}:{h.end})" for h in self.hits
        )


# 模式定义 —— 越严越好,宁愿误伤
# 命名风格:类别_细节,便于报告与调试
PATTERNS: dict[str, str] = {
    # --- 货币 ---
    "money_symbol_prefix": r"[¥$€£]\s*\d[\d,]*(?:\.\d+)?",
    "money_chinese_suffix": r"\d[\d,]*(?:\.\d+)?\s*(?:元|万|亿|千万|百万|千)",
    "money_iso_suffix": r"\d[\d,]*(?:\.\d+)?\s*(?:USD|CNY|RMB|EUR|GBP|JPY|HKD)",

    # --- 百分比 ---
    "percent": r"\d+(?:\.\d+)?\s*%",
    "percent_chinese": r"百分之[零一二三四五六七八九十百0-9]+",

    # --- 日期 ---
    "date_iso": r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b",
    "date_chinese_ymd": r"\d{4}\s*年\s*\d{1,2}\s*月(?:\s*\d{1,2}\s*日)?",
    "date_chinese_md": r"\d{1,2}\s*月\s*\d{1,2}\s*日",
    "date_chinese_year": r"\d{4}\s*年",
    "date_quarter": r"\d{4}\s*[年]?\s*Q[1-4]",
    "date_chinese_quarter": r"第\s*[一二三四1-4]\s*季度",

    # --- 长数字(4+ 位,排除明显的常见无害数字如 sheet 名)---
    # 注意 lookahead/lookbehind 排除被字母包裹的 ID 类
    "long_number": r"(?<![A-Za-z_])\d{4,}(?![A-Za-z_])",

    # --- 小数(可能是统计量)---
    "decimal": r"(?<!\d)\d+\.\d+",

    # --- 千分位 ---
    "thousands": r"\d{1,3}(?:,\d{3})+",
}


def _compile_patterns() -> dict[str, re.Pattern]:
    return {name: re.compile(p) for name, p in PATTERNS.items()}


_COMPILED = _compile_patterns()


def scan_summary(
    summary: str,
    *,
    extra_blocklist: Optional[list[str]] = None,
) -> FilterResult:
    """
    扫描 summary,检测疑似明文数据(B6 第 3 条)。

    Args:
        summary: LLM 输出的 summary 文本
        extra_blocklist: 额外的字面词黑名单(如 schema 中已知的 category 值)

    Returns:
        FilterResult:clean=True 表示无命中可放行;否则需重生成
    """
    hits: list[FilterHit] = []

    for name, pattern in _COMPILED.items():
        for m in pattern.finditer(summary):
            hits.append(
                FilterHit(
                    pattern_name=name,
                    matched_text=m.group(0),
                    start=m.start(),
                    end=m.end(),
                )
            )

    if extra_blocklist:
        for word in extra_blocklist:
            if not word:
                continue
            for m in re.finditer(re.escape(word), summary):
                hits.append(
                    FilterHit(
                        pattern_name="blocklist_word",
                        matched_text=m.group(0),
                        start=m.start(),
                        end=m.end(),
                    )
                )

    return FilterResult(clean=len(hits) == 0, hits=hits)


# 兜底范式回复 —— summary 反复命中时使用
FALLBACK_SUMMARY = (
    "分析已完成,具体数据与图表请打开本次生成的 Excel 文件查看。"
    "(为保护数据隐私,聊天界面不会显示任何具体内容)"
)
