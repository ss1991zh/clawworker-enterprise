"""主机 TLS 证书生成 + 客户端 TOFU 指纹锁定测试。"""
from __future__ import annotations

import ssl
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from host import gateway, tls_cert
from host.admin_ui import build_admin_router
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


def test_gateway_builds_loopback_http_and_lan_https(tmp_path):
    local_server, lan_server = gateway.build_servers(tmp_path)

    assert local_server.config.host == "127.0.0.1"
    assert local_server.config.port == 8442
    assert local_server.config.lifespan == "off"
    assert local_server.config.ssl_keyfile is None
    assert local_server.config.ssl_certfile is None

    assert lan_server.config.host == "0.0.0.0"
    assert lan_server.config.port == 8443
    assert lan_server.config.lifespan == "on"
    assert Path(lan_server.config.ssl_keyfile).exists()
    assert Path(lan_server.config.ssl_certfile).exists()


def test_admin_cookie_secure_flag_follows_request_scheme():
    class _Auth:
        def verify_login(self, username, password):
            return True

        def login(self):
            return "test-token"

    app = FastAPI()
    app.include_router(build_admin_router(
        auth_manager=None,
        user_manager=None,
        dispatcher=None,
        llm_config_store=None,
        provider_manager=None,
        call_stats=None,
        admin_auth=_Auth(),
    ))

    with TestClient(app, base_url="http://127.0.0.1:8442") as client:
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "ok"},
            follow_redirects=False,
        )
        assert "secure" not in response.headers["set-cookie"].lower()

    with TestClient(app, base_url="https://host.example:8443") as client:
        response = client.post(
            "/admin/login",
            data={"username": "admin", "password": "ok"},
            follow_redirects=False,
        )
        assert "secure" in response.headers["set-cookie"].lower()


def test_to_https():
    assert host_trust.to_https("http://192.168.1.5:8443") == "https://192.168.1.5:8443"
    assert host_trust.to_https("192.168.1.5:8443") == "https://192.168.1.5:8443"
    assert host_trust.to_https("https://h:8443") == "https://h:8443"


def test_verify_for_returns_pinned_ssl_context(tmp_path, monkeypatch):
    monkeypatch.setattr(host_trust, "_PIN_DIR", tmp_path / "pins")
    cert, _, _ = tls_cert.generate(tmp_path / "certificate")
    url = "https://127.0.0.1:8443"
    pin = host_trust._pin_file(url)
    pin.parent.mkdir(parents=True, exist_ok=True)
    pin.write_bytes(cert.read_bytes())

    context = host_trust.verify_for(url)

    assert isinstance(context, ssl.SSLContext)
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def test_client_host_url_is_forced_to_lan_https_port():
    assert host_trust.to_lan_https("http://192.168.1.5:8442") == "https://192.168.1.5:8443"
    assert host_trust.to_lan_https("http://192.168.1.5:8443/admin") == "https://192.168.1.5:8443"
    assert host_trust.to_lan_https("192.168.1.5") == "https://192.168.1.5:8443"


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
