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

    def raw_chat(self, system: str, user: str) -> str:
        """
        返回模型原始文本(不 parse 出 computation_plan)。
        默认实现:不支持(子类必须重写)。用于自由聊天场景。
        """
        raise NotImplementedError("provider 不支持 raw_chat")


# ---------------------------------------------------------------------------
# Stub Provider —— 测试与无 API key 场景
# ---------------------------------------------------------------------------


class StubLLMProvider(LLMProvider):
    """
    根据 user message 关键词返回预设响应。仅用于联调与测试。
    """

    def __init__(self, response: LLMResponse):
        self._response = response
        # 调用方约定:每次 chat 完毕后,provider.last_usage 应是
        # {"prompt_tokens": int, "completion_tokens": int}。stub 模拟一份非零值
        # 以便统计页面在 stub 模式下也能看到数字。
        self.last_usage: dict[str, int] = {}

    def chat(self, system: str, user: str) -> LLMResponse:
        # 简单按字符数估 tokens(粗略 1:4)
        pt = max(1, (len(system) + len(user)) // 4)
        ct = 200  # 假定固定输出长度
        self.last_usage = {"prompt_tokens": pt, "completion_tokens": ct}
        return self._response

    def raw_chat(self, system: str, user: str) -> str:
        pt = max(1, (len(system) + len(user)) // 4)
        ct = 30
        self.last_usage = {"prompt_tokens": pt, "completion_tokens": ct}
        return "(stub 模式 · 来自测试 provider 的回复 · 请配置真实 LLM)"


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
        self.last_usage: dict[str, int] = {}

    def _complete(self, system: str, user: str) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        # 抓 token 用量(Anthropic 返回 input_tokens / output_tokens)
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.last_usage = {
                "prompt_tokens": getattr(usage, "input_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "output_tokens", 0) or 0,
            }
        else:
            self.last_usage = {}
        return text

    def chat(self, system: str, user: str) -> LLMResponse:
        return parse_llm_text(self._complete(system, user))

    def raw_chat(self, system: str, user: str) -> str:
        return self._complete(system, user)


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
        # 每次调用后填:{"prompt_tokens", "completion_tokens", 可选 "cost_usd"}
        # cost_usd 在 OpenRouter 时来自服务端返回的 usage.cost(优先),否则按 PRICE_PER_1K 估
        self.last_usage: dict[str, float] = {}

    def _complete(self, system: str, user: str) -> str:
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
        # 抓 token 用量
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self.last_usage = {
                "prompt_tokens": getattr(usage, "prompt_tokens", 0) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", 0) or 0,
            }
            # OpenRouter 在 usage 里附带 cost 字段(美元)
            cost = getattr(usage, "cost", None)
            if cost is not None:
                self.last_usage["cost_usd"] = float(cost)
        else:
            self.last_usage = {}
        return text

    def chat(self, system: str, user: str) -> LLMResponse:
        return parse_llm_text(self._complete(system, user))

    def raw_chat(self, system: str, user: str) -> str:
        return self._complete(system, user)


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


# 国内厂商默认 base_url(给 make_provider 在没传 base_url 时兜底)
_DEFAULT_BASE_URLS: dict[str, str] = {
    "openai":      "https://api.openai.com/v1",
    "openrouter":  "https://openrouter.ai/api/v1",
    "deepseek":    "https://api.deepseek.com/v1",
    "zhipu":       "https://open.bigmodel.cn/api/paas/v4",
    "moonshot":    "https://api.moonshot.cn/v1",
    "qwen":        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "doubao":      "https://ark.cn-beijing.volces.com/api/v3",
    "hunyuan":     "https://api.hunyuan.cloud.tencent.com/v1",
    "baichuan":    "https://api.baichuan-ai.com/v1",
    "minimax":     "https://api.minimax.chat/v1",
    "stepfun":     "https://api.stepfun.com/v1",
    "spark":       "https://spark-api-open.xf-yun.com/v1",
    "baidu":       "https://qianfan.baidubce.com/v2",
    "sensetime":   "https://api.sensenova.cn/compatible-mode/v1",
    "yi":          "https://api.lingyiwanwu.com/v1",
    "siliconflow": "https://api.siliconflow.cn/v1",
}

# 走 OpenAI 兼容协议的 provider 类型(包括所有国内厂商)
_OPENAI_COMPATIBLE = set(_DEFAULT_BASE_URLS.keys())


def make_provider(model_type: str, **kwargs: Any) -> LLMProvider:
    """
    根据 model_type 创建对应 provider。

    支持:
    - "stub"        - 测试用,kwargs["response"]: LLMResponse
    - "anthropic"   - Anthropic Claude,kwargs["api_key"] + kwargs["model"]
    - openai 兼容协议:openai / openrouter / deepseek / zhipu / moonshot /
                      qwen / doubao / hunyuan / baichuan / minimax / stepfun /
                      spark / baidu / sensetime / yi / siliconflow
      kwargs["api_key"] + kwargs["model"](+ 可选 base_url 覆盖默认)
    """
    if model_type == "stub":
        return StubLLMProvider(kwargs["response"])
    if model_type == "anthropic":
        return AnthropicLLMProvider(
            api_key=kwargs["api_key"],
            model=kwargs.get("model", "claude-sonnet-4-5"),
        )
    if model_type in _OPENAI_COMPATIBLE:
        return OpenAICompatibleProvider(
            api_key=kwargs["api_key"],
            model=kwargs["model"],
            base_url=kwargs.get("base_url") or _DEFAULT_BASE_URLS[model_type],
            max_tokens=kwargs.get("max_tokens", 4096),
            temperature=kwargs.get("temperature", 0.2),
        )
    raise ValueError(f"暂不支持的 model_type: {model_type}")
