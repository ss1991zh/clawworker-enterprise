from __future__ import annotations

import httpx
import pytest

from client import host_trust
from client.host_client import (
    HostClient,
    HostConnectionError,
    HostResponseError,
    HostSession,
    cancellable_json_post,
)


class _FakeClient:
    def __init__(self, captured, result):
        self.captured = captured
        self.result = result
        self.closed = False

    def request(self, method, url, **kwargs):
        self.captured.update(method=method, url=url, **kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result

    def close(self):
        self.closed = True


def _client(captured=None, result=None) -> HostClient:
    captured = captured if captured is not None else {}
    result = result if result is not None else httpx.Response(200, json={"ok": True})

    def factory(**kwargs):
        captured["client_options"] = kwargs
        return _FakeClient(captured, result)

    return HostClient(
        lambda: HostSession("https://192.168.3.10:8443/", "token-1"),
        client_factory=factory,
    )


def test_request_json_applies_shared_auth_tls_proxy_and_timeout(monkeypatch):
    captured = {}
    monkeypatch.setattr(host_trust, "verify_for", lambda url: "pinned-ca.pem")
    monkeypatch.setattr(host_trust, "trust_revision", lambda url: "revision-1")

    assert _client(captured).request_json("POST", "/data/query", json_body={"x": 1}, timeout=90) == {
        "ok": True,
    }
    assert captured["method"] == "POST"
    assert captured["url"] == "/data/query"
    assert captured["client_options"]["base_url"] == "https://192.168.3.10:8443"
    assert captured["client_options"]["verify"] == "pinned-ca.pem"
    assert captured["client_options"]["trust_env"] is False
    assert captured["headers"] == {"Authorization": "Bearer token-1"}
    assert captured["timeout"].connect == 5.0
    assert captured["timeout"].read == 90


def test_request_json_preserves_management_error_detail(monkeypatch):
    monkeypatch.setattr(host_trust, "verify_for", lambda _url: False)
    monkeypatch.setattr(host_trust, "trust_revision", lambda _url: "revision-1")

    with pytest.raises(HostResponseError) as caught:
        _client(result=httpx.Response(422, json={"detail": "字段未授权"})).request_json(
            "GET", "/data/sources"
        )
    assert caught.value.status_code == 422
    assert caught.value.detail == "字段未授权"


def test_request_json_normalizes_connection_failure(monkeypatch):
    monkeypatch.setattr(host_trust, "verify_for", lambda _url: False)
    monkeypatch.setattr(host_trust, "trust_revision", lambda _url: "revision-1")
    with pytest.raises(HostConnectionError, match="无法连接管理端"):
        _client(result=httpx.ConnectError("offline")).request_json("GET", "/healthz")


def test_request_json_reuses_connection_until_trust_revision_changes(monkeypatch):
    revisions = iter(["r1", "r1", "r2"])
    monkeypatch.setattr(host_trust, "trust_revision", lambda _url: next(revisions))
    monkeypatch.setattr(host_trust, "verify_for", lambda _url: False)
    created = []

    def factory(**_kwargs):
        client = _FakeClient({}, httpx.Response(200, json={"ok": True}))
        created.append(client)
        return client

    client = HostClient(
        lambda: HostSession("https://host:8443", "token"),
        client_factory=factory,
    )
    client.request_json("GET", "/one")
    client.request_json("GET", "/two")
    client.request_json("GET", "/three")

    assert len(created) == 2
    assert created[0].closed is True


def test_request_bytes_uses_same_pool_and_returns_binary_body(monkeypatch):
    monkeypatch.setattr(host_trust, "trust_revision", lambda _url: "r1")
    monkeypatch.setattr(host_trust, "verify_for", lambda _url: False)

    result = _client(result=httpx.Response(200, content=b"authorization")).request_bytes(
        "GET", "/auth/user_authorization"
    )

    assert result == b"authorization"


def test_cancellable_json_post_uses_same_management_error_contract(monkeypatch):
    response = httpx.Response(422, json={"detail": "查询字段未授权"})
    monkeypatch.setattr(
        "client.host_client.cancellable_post",
        lambda *_args, **_kwargs: response,
    )
    with pytest.raises(HostResponseError) as caught:
        cancellable_json_post(
            "https://host:8443/llm/chat",
            headers={},
            json_body={},
            timeout=60,
        )
    assert caught.value.status_code == 422
    assert caught.value.detail == "查询字段未授权"


def test_cancellable_json_post_rejects_non_json_success(monkeypatch):
    response = httpx.Response(200, text="not-json")
    monkeypatch.setattr(
        "client.host_client.cancellable_post",
        lambda *_args, **_kwargs: response,
    )
    with pytest.raises(HostResponseError, match="无法识别"):
        cancellable_json_post(
            "https://host:8443/llm/chat",
            headers={},
            json_body={},
            timeout=60,
        )
