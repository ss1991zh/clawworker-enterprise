"""
LLM 客户端:与主机 LLM 代理通信。

抽象成 Protocol,方便测试中替换为 MockLLMClient。
"""

from __future__ import annotations

from typing import Protocol

import httpx

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
        self._client = httpx.Client(
            timeout=httpx.Timeout(timeout, connect=5.0)
        )

    def chat(self, system: str, user: str) -> LLMResponse:
        resp = self._client.post(
            f"{self.host_url}/llm/chat",
            headers={"Authorization": f"Bearer {self.session_token}"},
            json={"system": system, "user": user},
        )
        resp.raise_for_status()
        return LLMResponse.model_validate(resp.json())
