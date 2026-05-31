"""
A3 LLM 代理(architecture.md §A3)。

职责:
- 转发请求到大模型 API
- 注入系统 prompt
- 统一管理 模型类型 + APIKey

接口:LLMProvider.chat(system, user) -> LLMResponse

MVP 提供两个 provider:
- StubLLMProvider:返回预设的 ComputationPlan + summary,用于测试
- AnthropicLLMProvider:对接 Anthropic Claude API(需用户提供 API key)

要接入 OpenAI 等其他模型,新增对应 Provider 即可。
"""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Optional

from shared.contract import ComputationPlan, LLMResponse


class LLMProvider(ABC):
    """LLM 调用接口 —— 主机内部使用。"""

    @abstractmethod
    def chat(self, system: str, user: str) -> LLMResponse: ...


# ---------------------------------------------------------------------------
# Stub Provider —— 测试与无 API key 场景
# ---------------------------------------------------------------------------


class StubLLMProvider(LLMProvider):
    """
    根据 user message 关键词返回预设响应。仅用于联调与测试。
    """

    def __init__(self, response: LLMResponse):
        self._response = response

    def chat(self, system: str, user: str) -> LLMResponse:
        return self._response


# ---------------------------------------------------------------------------
# Anthropic Claude Provider
# ---------------------------------------------------------------------------


class AnthropicLLMProvider(LLMProvider):
    """对接 Anthropic Claude API。需要 api_key + model。"""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5", max_tokens: int = 4096):
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError("未安装 anthropic 包,请 `pip install anthropic`") from e
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def chat(self, system: str, user: str) -> LLMResponse:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return parse_llm_text(text)


class OpenAICompatibleProvider(LLMProvider):
    """
    通用 OpenAI 兼容协议 provider —— 一份代码对接 OpenRouter / OpenAI / DeepSeek 官方 / 任何兼容服务。

    用法示例:
        # OpenRouter + DeepSeek V4 Pro
        OpenAICompatibleProvider(
            api_key=os.environ["OPENROUTER_API_KEY"],
            base_url="https://openrouter.ai/api/v1",
            model="deepseek/deepseek-v4-pro",
        )
        # OpenAI 官方
        OpenAICompatibleProvider(
            api_key=os.environ["OPENAI_API_KEY"],
            model="gpt-4o",
        )
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://api.openai.com/v1",
        max_tokens: int = 4096,
        temperature: float = 0.2,
    ):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise RuntimeError("未安装 openai 包,请 `pip install openai`") from e
        self._client = OpenAI(api_key=api_key, base_url=base_url)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature

    def chat(self, system: str, user: str) -> LLMResponse:
        resp = self._client.chat.completions.create(
            model=self._model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=self._max_tokens,
            temperature=self._temperature,
        )
        text = resp.choices[0].message.content or ""
        return parse_llm_text(text)


# OpenRouter 是 OpenAI 兼容协议的代理,常用预设
def make_openrouter_provider(api_key: str, model: str, **kwargs: Any) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        api_key=api_key,
        model=model,
        base_url="https://openrouter.ai/api/v1",
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 文本响应解析(从 LLM 自由文本中提取 <computation_plan> 和 <summary>)
# ---------------------------------------------------------------------------


# 优先匹配 <computation_plan>...</computation_plan>(契约标准),
# 兜底匹配 markdown 标题【1. computation_plan】/ 【computation_plan】之后的 JSON 块
_PLAN_TAG_RE = re.compile(r"<computation_plan>\s*(\{.*?\})\s*</computation_plan>", re.DOTALL)
_SUMMARY_TAG_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)

# 兜底:中文【】或 markdown ## 风格 + json fence
_PLAN_FENCED_RE = re.compile(
    r"(?:【\s*\d*\.?\s*computation_plan\s*】|#+\s*computation_plan|computation_plan\s*[:：])"
    r".*?```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL | re.IGNORECASE,
)
_SUMMARY_MARK_RE = re.compile(
    r"(?:【\s*\d*\.?\s*summary\s*】|#+\s*summary|summary\s*[:：])\s*\n?(.*?)(?=【|^#+\s|\Z)",
    re.DOTALL | re.IGNORECASE | re.MULTILINE,
)

# 再兜底:整段里第一个 json 代码块(最宽松)
_ANY_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def parse_llm_text(text: str) -> LLMResponse:
    """
    把 LLM 的自由文本响应解析为 LLMResponse。

    按优先级三层兜底:
    1. <computation_plan>...</computation_plan>(契约标准)
    2. 【1. computation_plan】+ ```json``` 代码块
    3. 整段第一个 json 代码块 + 第一个【2. summary】或 <summary>
    """
    # ---- computation_plan ----
    plan_match = _PLAN_TAG_RE.search(text)
    if plan_match:
        plan_json = plan_match.group(1)
    else:
        plan_match = _PLAN_FENCED_RE.search(text)
        if plan_match:
            plan_json = plan_match.group(1)
        else:
            # 最宽松:整段第一个 json 块
            any_json = _ANY_JSON_FENCE_RE.search(text)
            if not any_json:
                raise ValueError("LLM 响应中未找到 computation_plan(无 JSON 块)")
            plan_json = any_json.group(1)

    try:
        plan_dict = json.loads(plan_json)
    except json.JSONDecodeError as e:
        raise ValueError(f"computation_plan 不是有效 JSON: {e}")
    plan = ComputationPlan.model_validate(plan_dict)

    # ---- summary ----
    summary_match = _SUMMARY_TAG_RE.search(text)
    if summary_match:
        summary = summary_match.group(1).strip()
    else:
        summary_match = _SUMMARY_MARK_RE.search(text)
        if summary_match:
            summary = summary_match.group(1).strip()
        else:
            raise ValueError("LLM 响应中未找到 summary")

    return LLMResponse(computation_plan=plan, summary=summary)


# ---------------------------------------------------------------------------
# Provider 工厂
# ---------------------------------------------------------------------------


def make_provider(model_type: str, **kwargs: Any) -> LLMProvider:
    """
    根据 model_type 创建对应 provider。

    支持:
    - "stub"        - 测试用,kwargs["response"]: LLMResponse
    - "anthropic"   - Anthropic Claude,kwargs["api_key"] + kwargs["model"]
    - "openrouter"  - OpenRouter,kwargs["api_key"] + kwargs["model"](如 deepseek/deepseek-v4-pro)
    - "openai"      - OpenAI 官方,kwargs["api_key"] + kwargs["model"]
    """
    if model_type == "stub":
        return StubLLMProvider(kwargs["response"])
    if model_type == "anthropic":
        return AnthropicLLMProvider(
            api_key=kwargs["api_key"],
            model=kwargs.get("model", "claude-sonnet-4-5"),
        )
    if model_type == "openrouter":
        return make_openrouter_provider(
            api_key=kwargs["api_key"],
            model=kwargs["model"],
            max_tokens=kwargs.get("max_tokens", 4096),
            temperature=kwargs.get("temperature", 0.2),
        )
    if model_type == "openai":
        return OpenAICompatibleProvider(
            api_key=kwargs["api_key"],
            model=kwargs["model"],
            base_url=kwargs.get("base_url", "https://api.openai.com/v1"),
            max_tokens=kwargs.get("max_tokens", 4096),
            temperature=kwargs.get("temperature", 0.2),
        )
    raise ValueError(f"暂不支持的 model_type: {model_type}")
