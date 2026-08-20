from __future__ import annotations

import pytest

from client.host_client import HostRequestCancelled, HostResponseError
from client.webui.llm_gateway import PipelineCancelledError, PipelineLLMGateway


def test_gateway_counts_call_and_usage_once(monkeypatch):
    events = []
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(url=url, **kwargs)
        kwargs["before_request"]()
        return {"text": "answer", "usage": {"total_tokens": 12}}

    monkeypatch.setattr("client.webui.llm_gateway.cancellable_json_post", fake_post)
    gateway = PipelineLLMGateway(
        before_call=lambda: events.append("call"),
        add_usage=lambda usage: events.append(usage["total_tokens"]),
    )

    result = gateway.freechat(
        "https://host:8443/", "token", "hello", web_search=True,
    )

    assert result == "answer"
    assert events == ["call", 12]
    assert captured["url"] == "https://host:8443/llm/freechat"
    assert captured["json_body"]["web_search"] is True


def test_gateway_maps_expired_session_to_permission_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise HostResponseError(401, "expired")

    monkeypatch.setattr("client.webui.llm_gateway.cancellable_json_post", fail)
    gateway = PipelineLLMGateway(before_call=lambda: None, add_usage=lambda _usage: None)

    with pytest.raises(PermissionError, match="登录已过期"):
        gateway.codegen("https://host:8443", "token", "system", "user")


def test_gateway_preserves_pipeline_cancellation(monkeypatch):
    def cancel(*_args, **_kwargs):
        raise HostRequestCancelled("用户已停止")

    monkeypatch.setattr("client.webui.llm_gateway.cancellable_json_post", cancel)
    gateway = PipelineLLMGateway(before_call=lambda: None, add_usage=lambda _usage: None)

    with pytest.raises(PipelineCancelledError, match="用户已停止"):
        gateway.freechat("https://host:8443", "token", "hello")
