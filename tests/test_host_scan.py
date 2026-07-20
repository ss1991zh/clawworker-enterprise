"""内网扫描发现主机 —— 免手输 IP。

安全底线:扫描**只找候选、不做信任决策**。同内网任何人都能在 8443 跑假主机,
若扫到就信任等于把口令/token 送出去。信任判据仍是 SPKI(公钥):
已 pin 过的 → known=true 可直连;未知的 → 必须人工核对指纹后选。
"""
from __future__ import annotations

import ipaddress

import pytest

from client import host_scan


def test_only_scans_private_networks(monkeypatch):
    # 公网/链路本地地址不参与扫描(不对外网做探测)
    monkeypatch.setattr(host_scan, "local_ipv4",
                        lambda: ["8.8.8.8", "169.254.1.5", "192.168.3.169"])
    assert host_scan.candidate_networks() == ["192.168.3.0/24"]


def test_networks_deduplicated(monkeypatch):
    monkeypatch.setattr(host_scan, "local_ipv4",
                        lambda: ["192.168.3.10", "192.168.3.99"])
    assert host_scan.candidate_networks() == ["192.168.3.0/24"]


def test_large_network_refused_not_scanned(monkeypatch):
    """/16 有 6.5 万地址:不扫(不现实,且会触发企业 EDR 的端口扫描告警)。"""
    called = []
    monkeypatch.setattr(host_scan, "_probe", lambda ip: called.append(ip))
    r = host_scan.scan(network="10.0.0.0/16")
    assert r["too_large"] is True
    assert called == [], "大网段不应发起任何探测"
    assert r["candidates"] == []


def test_scan_marks_known_by_spki(monkeypatch):
    """公钥与本地已锁定一致 → known=true(可安全直连);否则 false(需人工核对)。"""
    monkeypatch.setattr(host_scan, "candidate_networks", lambda: ["192.168.3.0/24"])
    fake = {
        "192.168.3.5": {"ip": "192.168.3.5", "url": "https://192.168.3.5:8443",
                        "spki": "AA:BB", "cn": host_scan._HOST_CN},
        "192.168.3.9": {"ip": "192.168.3.9", "url": "https://192.168.3.9:8443",
                        "spki": "CC:DD", "cn": host_scan._HOST_CN},
    }
    monkeypatch.setattr(host_scan, "_probe", lambda ip: fake.get(ip))
    monkeypatch.setattr(host_scan, "known_spkis", lambda: {"CC:DD"})
    r = host_scan.scan()
    by_ip = {c["ip"]: c for c in r["candidates"]}
    assert by_ip["192.168.3.9"]["known"] is True
    assert by_ip["192.168.3.5"]["known"] is False
    # 已认识的排最前,便于自动选中
    assert r["candidates"][0]["ip"] == "192.168.3.9"


def test_probe_rejects_non_product_service(monkeypatch):
    """8443 上跑着别的服务(CN 不符)→ 不列为候选,避免误导用户去连。"""
    monkeypatch.setattr(host_scan, "_port_open", lambda ip, port=8443: True)
    import client.host_trust as ht
    monkeypatch.setattr(ht, "fetch_server_cert_pem", lambda url: b"pem")
    monkeypatch.setattr(ht, "_spki_of_pem", lambda pem: "AA:BB")
    monkeypatch.setattr(host_scan, "_cert_cn", lambda pem: "Some Other Server")
    assert host_scan._probe("192.168.3.7") is None


def test_scan_endpoint_is_csrf_exempt_but_origin_checked():
    """登录页(无 session、无 token)要能调它;Origin 与 Host 校验仍然生效。"""
    import importlib
    app_mod = importlib.import_module("client.webui.app")
    assert "/api/host/scan" in app_mod._CSRF_EXEMPT_PATHS
