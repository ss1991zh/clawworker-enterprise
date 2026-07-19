"""
代码生成沙盒加固回归测试 —— 锁住门控对 LLM 常见误写的自动纠偏。

这些用例用 FakeCt + 仿 pandaseal 类型(不依赖真实 HE),覆盖:
  · 臆造方法名(encrypt_ndarray…)→ 映射真实 API
  · ct.decrypt 误吃 CipherSeries / CipherDataFrame → 转正确调用
  · ct.decrypt_df(cdf) 整表 → 自动拼回身份列
  · 真解密错 → DecryptionFailed(终态);取消/保留信号原样传出
  · import 白名单(time 放行 / os 拦截)
  · 渲染端同名列去重
"""
from __future__ import annotations

import pandas as pd
import pytest

from client.webui import codegen as cg


# —— 仿 pandaseal 类型(类名必须正好是 CipherSeries / CipherDataFrame)——
class CipherSeries:
    def __init__(self, name="col"):
        self.name = name

    def to_cipherarray(self):
        return ("ARRAY", self.name)

    def to_cipherdataframe(self):
        return CipherDataFrame(cols=[self.name])


class CipherDataFrame:
    def __init__(self, cols=("a", "b")):
        self.cols = list(cols)


class FakeCt:
    """记录每次解密/加密真正落到哪个方法 + 收到什么参数。"""

    def __init__(self, n_rows=3, raise_on_decrypt=False):
        self.calls = []
        self.n_rows = n_rows
        self.raise_on_decrypt = raise_on_decrypt

    def encrypt(self, x):
        self.calls.append(("encrypt", x))
        return ("ENC", x)

    def encrypt_df(self, df):
        self.calls.append(("encrypt_df", df))
        return ("ENC_DF", df)

    def decrypt(self, x, *a, **k):
        self.calls.append(("decrypt", x))
        if self.raise_on_decrypt:
            raise ValueError("real crypto error")
        return [1.0, 2.0, 3.0]

    def decrypt_df(self, c):
        self.calls.append(("decrypt_df", c))
        if self.raise_on_decrypt:
            raise ValueError("real crypto error")
        # 仅数值列(模拟 decrypt_df 不含身份列)
        return pd.DataFrame({"金额": list(range(self.n_rows))})


def _gate(real_ct, *, on_first=lambda: None, original_cdf=None, meta_df=None):
    return cg._CtGate(real_ct, on_first, original_cdf=original_cdf, meta_df=meta_df)


# ───────────────────────── 方法名纠偏 ─────────────────────────

@pytest.mark.parametrize("alias,real", [
    ("encrypt_ndarray", "encrypt"),
    ("encrypt_array", "encrypt"),
    ("encrypt_numpy", "encrypt"),
    ("decrypt_ndarray", "decrypt"),
    ("decrypt_array", "decrypt"),
    ("encrypt_dataframe", "encrypt_df"),
])
def test_hallucinated_method_names_redirect(alias, real):
    ct = FakeCt()
    g = _gate(ct)
    getattr(g, alias)([1, 2, 3])
    assert ct.calls and ct.calls[-1][0] == real


def test_unknown_method_still_raises_attributeerror():
    g = _gate(FakeCt())
    with pytest.raises(AttributeError):
        g.totally_made_up_method(1)


# ─────────────────── pandaseal 类型解密纠偏 ───────────────────

def test_decrypt_cipherseries_uses_to_cipherarray():
    ct = FakeCt()
    g = _gate(ct)
    g.decrypt(CipherSeries("销售额"))
    # 真正落到 decrypt,且参数是 to_cipherarray() 的结果
    assert ("decrypt", ("ARRAY", "销售额")) in ct.calls


def test_decrypt_cipherdataframe_routes_to_decrypt_df():
    ct = FakeCt()
    g = _gate(ct)
    out = g.decrypt(CipherDataFrame())
    assert any(c[0] == "decrypt_df" for c in ct.calls)
    assert isinstance(out, pd.DataFrame)


def test_decrypt_df_cipherseries_converts_first():
    ct = FakeCt()
    g = _gate(ct)
    g.decrypt_df(CipherSeries("x"))
    # decrypt_df 收到的是 CipherDataFrame(由 to_cipherdataframe 转出)
    assert any(c[0] == "decrypt_df" and isinstance(c[1], CipherDataFrame) for c in ct.calls)


# ─────────────────── 整表解密自动拼回身份列 ───────────────────

def test_decrypt_df_attaches_identity_columns():
    ct = FakeCt(n_rows=3)
    cdf = object()  # 原始 cdf 哨兵
    meta = pd.DataFrame({"销售大区": ["华东", "华南", "华北"], "月份": ["1", "2", "3"]})
    g = _gate(ct, original_cdf=cdf, meta_df=meta)
    full = g.decrypt_df(cdf)
    assert list(full.columns) == ["销售大区", "月份", "金额"]   # 身份列在前
    assert full["销售大区"].tolist() == ["华东", "华南", "华北"]


def test_identity_not_attached_to_subframe():
    ct = FakeCt(n_rows=3)
    cdf = object()
    meta = pd.DataFrame({"销售大区": ["华东", "华南", "华北"]})
    g = _gate(ct, original_cdf=cdf, meta_df=meta)
    # 解密的不是原始 cdf(是别的 CipherDataFrame)→ 不拼身份列
    out = g.decrypt_df(CipherDataFrame())
    assert "销售大区" not in out.columns


def test_identity_attach_skips_on_rowcount_mismatch():
    ct = FakeCt(n_rows=5)  # 解密 5 行
    cdf = object()
    meta = pd.DataFrame({"销售大区": ["华东", "华南"]})  # 仅 2 行 → 不拼
    g = _gate(ct, original_cdf=cdf, meta_df=meta)
    full = g.decrypt_df(cdf)
    assert "销售大区" not in full.columns


# ─────────────────── 终态错误 / 信号 ───────────────────

def test_real_decrypt_error_becomes_decryptionfailed():
    g = _gate(FakeCt(raise_on_decrypt=True))
    with pytest.raises(cg.DecryptionFailed):
        g.decrypt([0.1, 0.2])


def test_cancel_signal_passes_through():
    def on_first():
        raise cg.CodegenCancelled("stop")
    g = _gate(FakeCt(), on_first=on_first)
    with pytest.raises(cg.CodegenCancelled):
        g.decrypt([0.1])


def test_keep_encrypted_signal_passes_through():
    def on_first():
        raise cg.KeepEncrypted("keep")
    g = _gate(FakeCt(), on_first=on_first)
    with pytest.raises(cg.KeepEncrypted):
        g.decrypt([0.1])


# ─────────────────── import 白名单 ───────────────────

@pytest.mark.parametrize("mod", ["time", "math", "calendar", "decimal", "random", "operator"])
def test_safe_imports_allowed(mod):
    cg.ast_safety_check(f"import {mod}\nx = 1")


@pytest.mark.parametrize("code", [
    "import os",
    "import sys",
    "import subprocess",
    "import socket",
    "from pathlib import Path",
    "import pickle",
])
def test_dangerous_imports_blocked(code):
    with pytest.raises(cg.UnsafeCode):
        cg.ast_safety_check(code)


# ─────────────────── 沙盒逃逸面加固(反射 / 格式串)───────────────────

@pytest.mark.parametrize("code", [
    'x = getattr((), "__class__")',                       # getattr 字符串实参逃逸
    'x = getattr(obj, "__globals__")',
    'x = "{0.__class__.__init__.__globals__}".format(())', # str.format 格式串逃逸
    'x = "{0.__class__}".format(obj)',
    'x = "hi".format_map({})',                             # format_map 同理
    'x = type(())("").__class__',                          # 字面量 dunder(既有防线)
    'x = ().__class__.__bases__',
    'x = obj.__globals__',
    'x = pd.read_pickle("/etc/passwd")',                   # 反序列化入口
    'x = df.to_pickle("/tmp/x")',
])
def test_sandbox_escape_vectors_blocked(code):
    with pytest.raises(cg.UnsafeCode):
        cg.ast_safety_check(code)


def test_getattr_format_not_in_safe_builtins():
    b = cg._safe_builtins(lambda *a, **k: None)
    assert "getattr" not in b and "format" not in b


@pytest.mark.parametrize("code", [
    'df = ct.decrypt_df(cdf)\nout = df.groupby("大区")["额"].sum()',   # 正常分析不误伤
    'x = round(1.23456, 2)\ny = f"{x:.2f}"',                          # f-string 数值格式
    's = "回款率" + "(加权)"\nn = len([1, 2, 3])',                     # 普通字符串拼接
])
def test_legit_analysis_code_still_passes(code):
    cg.ast_safety_check(code)   # 不应抛


# ─────────────────── 渲染端去重兜底 ───────────────────

def test_writer_dedups_duplicate_columns():
    from client.webui import writer as W
    df = pd.DataFrame([[1, "a", 1], [2, "b", 2]], columns=["金额", "区域", "金额"])
    out = W._dedup_columns(df)
    assert list(out.columns) == ["金额", "区域"]
