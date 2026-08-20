"""用户端只能把管理主机规范化为局域网 HTTPS 入口。"""
from __future__ import annotations

import importlib
from pathlib import Path
import pytest


app_mod = importlib.import_module("client.webui.app")


def test_old_local_admin_url_is_migrated_to_lan_https():
    assert (
        app_mod._validate_host_url("http://192.168.3.169:8442/admin")
        == "https://192.168.3.169:8443"
    )


def test_manual_ip_defaults_to_lan_https():
    assert app_mod._validate_host_url("192.168.3.169") == "https://192.168.3.169:8443"


@pytest.mark.parametrize(
    "bad",
    [
        "https://user:password@192.168.3.169:8443",
        "https://8.8.8.8:8443",
        "https://169.254.169.254:8443",
    ],
)
def test_invalid_or_public_host_is_rejected(bad):
    with pytest.raises(ValueError):
        app_mod._validate_host_url(bad)


def test_login_page_explains_lan_https_port():
    template = (
        Path(app_mod.__file__).resolve().parent / "templates" / "login.html"
    ).read_text(encoding="utf-8")
    assert "局域网 HTTPS 8443" in template
    assert "管理端IP:8443" in template


def test_multiple_scan_candidates_are_never_auto_selected():
    """局域网有多台管理端时，不能因其中一台曾受信任就静默选错目标。"""
    template = (
        Path(app_mod.__file__).resolve().parent / "templates" / "login.html"
    ).read_text(encoding="utf-8")
    assert "cs.length === 1 && known.length === 1" in template
    assert "c.local" in template
    assert "请点击要连接的那一台" in template


def test_login_failure_names_actual_host_and_explains_client_account():
    message = app_mod._format_login_failure(
        "https://192.168.3.146:8443", "账户不存在"
    )
    assert "192.168.3.146:8443" in message
    assert "用户管理" in message
    assert "不是管理端登录账号" in message


def test_database_proxy_uses_pinned_host_certificate(monkeypatch):
    """数据库页的代理请求必须加载 TLS 信任模块，不能因缺失变量返回 500。"""
    seen = {}

    def fake_request(method, path, **kwargs):
        seen.update(method=method, path=path, **kwargs)
        return [{"id": "erp", "name": "ERP"}]

    monkeypatch.setattr(app_mod, "_session_state", {
        "host_url": "https://192.168.3.169:8443",
        "username": "alice", "token": "session-token", "expires_at": "",
    })
    monkeypatch.setattr(app_mod._host_client, "request_json", fake_request)

    result = app_mod._host_api("GET", "/data/sources")

    assert result == [{"id": "erp", "name": "ERP"}]
    assert seen["path"] == "/data/sources"
    assert app_mod._current_host_session().base_url == "https://192.168.3.169:8443"
    assert app_mod._current_host_session().token == "session-token"
