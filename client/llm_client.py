"""
LLM 客户端:与主机 LLM 代理通信。

抽象成 Protocol,方便测试中替换为 MockLLMClient。
"""

from __future__ import annotations

from typing import Protocol

from client.host_client import HostClient, HostSession
from shared.contract import LLMResponse


class LLMClient(Protocol):
    """LLM 调用接口。"""

    def chat(self, system: str, user: str) -> LLMResponse: ...


class HTTPLLMClient:
    """
    生产实现:通过 HTTP 调用主机 LLM 代理。

    主机侧实现见 host/llm_proxy.py 和 host/server.py。
    """

    def __init__(self, host_url: str, session_token: str, timeout: float = 180.0):
        # 默认 180s:推理型模型(deepseek-v4-pro / o1 等)通常思考时间长,
        # 60s 经常 timeout。同时 connect 5s 防 host 完全不可达时空等。
        self.host_url = host_url.rstrip("/")
        self.session_token = session_token
        self.timeout = timeout
        self._client = HostClient(
            lambda: HostSession(self.host_url, self.session_token),
        )

    def chat(self, system: str, user: str) -> LLMResponse:
        body = self._client.request_json(
            "POST",
            "/llm/chat",
            json_body={"system": system, "user": user},
            timeout=self.timeout,
        )
        return LLMResponse.model_validate(body)

    def close(self) -> None:
        self._client.close()
