"""SPKI(公钥)指纹信任 —— 修「主机换网络后客户端被误报中间人」。

背景:主机换网络 → 本机 IP 变 → 证书 SAN 不覆盖 → 自动重签 → 整证书指纹必变。
旧实现按整证书指纹判定,合法重签会被当成"疑似中间人"拦住(演示途中最致命)。
改法:主机重签**复用私钥**,客户端按 **SPKI(公钥)** 判身份 —— 重签放行、换主机仍拦。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from host import tls_cert as tc


def test_reissue_reuses_key_so_spki_stable():
    d = Path(tempfile.mkdtemp())
    c1, k1, f1 = tc.generate(d)
    spki1, key1 = tc.spki_fingerprint(c1), k1.read_bytes()
    c2, k2, f2 = tc.generate(d)          # 模拟换网导致的重签
    assert f2 != f1, "重签后整证书指纹应当变化"
    assert tc.spki_fingerprint(c2) == spki1, "复用私钥时公钥指纹不应变"
    assert k2.read_bytes() == key1, "私钥应被复用而非重新生成"


def test_new_key_changes_spki():
    a, b = Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    ca, _, _ = tc.generate(a)
    cb, _, _ = tc.generate(b)            # 另一台主机 = 另一把私钥
    assert tc.spki_fingerprint(ca) != tc.spki_fingerprint(cb)


def _pin(ht, url, pem_path):
    ht._PIN_DIR.mkdir(parents=True, exist_ok=True)
    ht._pin_file(url).write_bytes(pem_path.read_bytes())


def test_client_spki_helpers(monkeypatch, tmp_path):
    from client import host_trust as ht
    monkeypatch.setattr(ht, "_PIN_DIR", tmp_path / "pins")
    d = Path(tempfile.mkdtemp())
    c1, _, _ = tc.generate(d)
    url = "https://192.168.1.5:8443"
    _pin(ht, url, c1)
    assert ht.pinned_spki(url) == tc.spki_fingerprint(c1)
    # 重签(复用私钥)后 pin 里的旧证书,其 SPKI 仍与新证书一致
    c2, _, _ = tc.generate(d)
    assert ht.pinned_spki(url) == tc.spki_fingerprint(c2)
    assert ht.pinned_fingerprint(url) != tc.fingerprint(c2)   # 整证书指纹确实变了


def test_heal_requires_matching_spki(monkeypatch, tmp_path):
    """自愈只认公钥一致;拿不到 pin 或公钥不符一律不放行。"""
    from client import host_trust as ht
    monkeypatch.setattr(ht, "_PIN_DIR", tmp_path / "pins")
    url = "https://192.168.1.9:8443"
    # 没有 pin → 不自愈(不能把陌生主机当成"原来那台")
    assert ht.heal_if_same_host(url) is False

    d = Path(tempfile.mkdtemp())
    c1, _, _ = tc.generate(d)
    _pin(ht, url, c1)
    other = Path(tempfile.mkdtemp())
    c_other, _, _ = tc.generate(other)
    # 服务端换成另一把密钥 → 公钥不符 → 拒绝自愈
    monkeypatch.setattr(ht, "server_spki", lambda u: tc.spki_fingerprint(c_other))
    assert ht.heal_if_same_host(url) is False
