"""吊销闭环:解密前会话新鲜度门(优化循环 T0-6)。

客户端本地持有 sk,解密纯本地发生,主机无法实时阻止离线解密。用会话 TTL 作
「短 TTL 强制回主机」的吊销闭环:会话过期(expires_at 过)→ 解密被拦,须回主机重登,
主机在登录时应用吊销/禁用,把离线解密窗口收敛到会话 TTL(默认 8h)。
详见 docs/revocation-model.md。
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta

import pytest

app_mod = importlib.import_module("client.webui.app")


@pytest.fixture(autouse=True)
def _restore_session():
    snap = dict(app_mod._session_state)
    yield
    app_mod._session_state.clear()
    app_mod._session_state.update(snap)


def _set_session(expires_at):
    app_mod._session_state.update(
        {"host_url": "https://127.0.0.1:8443", "username": "u",
         "token": "t", "expires_at": expires_at})


def test_fresh_when_expiry_in_future():
    _set_session((datetime.now() + timedelta(hours=2)).isoformat())
    assert app_mod._session_fresh() is True


def test_stale_when_expiry_passed():
    _set_session((datetime.now() - timedelta(minutes=1)).isoformat())
    assert app_mod._session_fresh() is False


def test_stale_when_no_expiry():
    _set_session("")
    assert app_mod._session_fresh() is False


def test_stale_when_malformed_expiry():
    _set_session("not-a-timestamp")
    assert app_mod._session_fresh() is False


def test_need_revalidate_shape():
    r = app_mod._need_revalidate()
    assert r.status_code == 401
    import json
    body = json.loads(bytes(r.body).decode())
    assert body["error"] == "session_stale"
