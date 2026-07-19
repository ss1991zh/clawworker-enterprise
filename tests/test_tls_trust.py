"""主机 TLS 证书生成 + 客户端 TOFU 指纹锁定测试。"""
from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from host import tls_cert
from client import host_trust


def test_cert_generate_and_reuse(tmp_path):
    cert, key, fp = tls_cert.ensure_cert(tmp_path)
    assert cert.exists() and key.exists()
    assert len(fp) == 95 and fp.count(":") == 31   # 32 字节 sha256 冒号十六进制
    # ssl 能加载
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert), str(key))
    # 复用:指纹不变
    _, _, fp2 = tls_cert.ensure_cert(tmp_path)
    assert fp == fp2


def test_cert_san_covers_localhost(tmp_path):
    cert, _, _ = tls_cert.generate(tmp_path)
    assert tls_cert._cert_covers_current_ips(cert)   # SAN 含本机 IP


def test_to_https():
    assert host_trust.to_https("http://192.168.1.5:8443") == "https://192.168.1.5:8443"
    assert host_trust.to_https("192.168.1.5:8443") == "https://192.168.1.5:8443"
    assert host_trust.to_https("https://h:8443") == "https://h:8443"


def test_tofu_pin_and_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(host_trust, "_PIN_DIR", tmp_path / "pins")
    # 造两张不同证书的 PEM
    c1, _, fp1 = tls_cert.generate(tmp_path / "a")
    c2, _, fp2 = tls_cert.generate(tmp_path / "b")
    assert fp1 != fp2
    url = "https://10.0.0.9:8443"
    # 手动写入 pin(模拟 TOFU 首锁 cert1)
    host_trust._PIN_DIR.mkdir(parents=True, exist_ok=True)
    host_trust._pin_file(url).write_bytes(c1.read_bytes())
    assert host_trust.pinned_fingerprint(url) == fp1
    # 指纹按 PEM 计算,和 host 侧一致
    assert host_trust._fp_of_pem(c1.read_bytes()) == fp1
