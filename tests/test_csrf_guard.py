"""客户端 CSRF / DNS-rebinding 中间件回归测试。"""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

app_mod = importlib.import_module("client.webui.app")

client = TestClient(app_mod.app)
GOOD_HOST = "127.0.0.1:8444"
TOKEN = app_mod._CSRF_TOKEN


def test_bad_host_header_rejected():
    # DNS-rebinding:浏览器带攻击者域名的 Host
    r = client.get("/api/sessions", headers={"Host": "evil.example.com"})
    assert r.status_code == 403
    assert "Host" in r.json()["detail"]


def test_safe_get_with_good_host_passes_guard():
    # 通过 Host + CSRF 闸门(未登录会 401,但不是 403)
    r = client.get("/api/sessions", headers={"Host": GOOD_HOST})
    assert r.status_code != 403


def test_mutation_without_csrf_token_rejected():
    r = client.post("/api/config", headers={"Host": GOOD_HOST}, json={"backend": "stub"})
    assert r.status_code == 403
    assert "CSRF" in r.json()["detail"]


def test_mutation_with_csrf_token_passes_guard():
    r = client.post("/api/config", headers={"Host": GOOD_HOST, "X-CSRF-Token": TOKEN},
                    json={"backend": "stub"})
    assert r.status_code != 403   # 过了 CSRF 闸门(可能 401 未登录)


def test_cross_site_origin_rejected():
    r = client.post("/api/config",
                    headers={"Host": GOOD_HOST, "X-CSRF-Token": TOKEN,
                             "Origin": "https://evil.example.com"},
                    json={"backend": "stub"})
    assert r.status_code == 403
    assert "跨站" in r.json()["detail"]


def test_login_form_exempt_from_csrf():
    # 登录表单尚无 session,豁免 CSRF token(仍受 Host 校验)
    r = client.post("/login", headers={"Host": GOOD_HOST},
                    data={"host_url": "http://127.0.0.1:8443", "username": "x", "password": "y"},
                    follow_redirects=False)
    assert r.status_code != 403


def test_login_cross_site_origin_rejected():
    # 登录 CSRF:跨站强制登入 —— 豁免 token 但仍须查 Origin
    r = client.post("/login",
                    headers={"Host": GOOD_HOST, "Origin": "https://evil.example.com"},
                    data={"host_url": "http://127.0.0.1:8443", "username": "x", "password": "y"},
                    follow_redirects=False)
    assert r.status_code == 403
