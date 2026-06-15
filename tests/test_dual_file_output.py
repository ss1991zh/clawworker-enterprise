"""
解密双文件 / 保留密文事后解密 —— 数据模型与沙盒解密的轻量回归(不依赖真实 HE)。
"""
from __future__ import annotations

import pytest

from client.webui.sessions import Message


def test_message_roundtrip_dual_file_fields():
    m = Message(
        id="m1", role="assistant", status="done", summary="ok",
        excel_path="/d/plain.xlsx", excel_name="plain.xlsx",
        enc_excel_path="/d/cipher.xlsx", enc_excel_name="cipher.xlsx",
        can_decrypt=True, dec_run_id="abc123", dec_stem="销售_回款率",
    )
    d = m.to_dict()
    m2 = Message.from_dict(d)
    assert m2.enc_excel_path == "/d/cipher.xlsx"
    assert m2.enc_excel_name == "cipher.xlsx"
    assert m2.can_decrypt is True
    assert m2.dec_run_id == "abc123"
    assert m2.dec_stem == "销售_回款率"


def test_message_defaults_backward_compat():
    # 旧消息(无新字段)反序列化 → 安全默认
    m = Message.from_dict({"id": "x", "role": "assistant", "excel_path": "/d/a.xlsx"})
    assert m.enc_excel_path == "" and m.can_decrypt is False and m.dec_run_id == ""


def test_decrypt_missing_run_raises():
    from client.webui import sched_results
    with pytest.raises(FileNotFoundError):
        sched_results.decrypt_persisted_run_to_excel("nonexistent_run_xyz", "x")
