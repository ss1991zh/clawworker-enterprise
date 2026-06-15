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
    def chat(self, system: str, user: str, web_search: bool = False) -> LLMResponse: ...

    def raw_chat(self, system: str, user: str, web_search: bool = False) -> str:
        """
        返回模型原始文本(不 parse 出 computation_plan)。
        默认实现:不支持(子类必须重写)。用于自由聊天场景。
        web_search=True:若该 provider/模型支持联网搜索就启用;不支持则自动降级为普通调用。
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

    def chat(self, system: str, user: str, web_search: bool = False) -> LLMResponse:
        # 简单按字符数估 tokens(粗略 1:4)
        pt = max(1, (len(system) + len(user)) // 4)
        ct = 200  # 假定固定输出长度
        self.last_usage = {"prompt_tokens": pt, "completion_tokens": ct}
        return self._response

    def raw_chat(self, system: str, user: str, web_search: bool = False) -> str:
        pt = max(1, (len(system) + len(user)) // 4)
        ct = 30
        self.last_usage = {"prompt_tokens": pt, "completion_tokens": ct}
        return "(stub 模式 · 来自测试 provider 的回复 · 请配置真实 LLM)"


# ---------------------------------------------------------------------------
# Anthropic Claude Provider
# ---------------------------------------------------------------------------


class AnthropicLLMProvider(LLMProvider):
    """对接 Anthropic Claude API。需要 api_key + model。"""

    def __init__(self, api_key: str, model: str = "claude-sonnet-4-5", max_tokens: int = 16000,
                 timeout: float = 1800.0):
        try:
            import anthropic  # type: ignore
        except ImportError as e:
            raise RuntimeError("未安装 anthropic 包,请 `pip install anthropic`") from e
        # timeout 单位秒 · 长任务(5-15 分钟级)不能默认 10 分钟就断
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout)
        self._model = model
        self._max_tokens = max_tokens
        self.last_usage: dict[str, int] = {}

    def _complete(self, system: str, user: str, web_search: bool = False) -> str:
        # 联网搜索:Anthropic 服务端 web_search 工具,API 自动跑搜索、最终文本带引用返回。
        # 模型不支持时(老模型)会 400 → 自动降级为普通调用。
        if web_search:
            try:
                return self._complete_with_search(system, user)
            except Exception:
                pass  # 降级
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

    def _complete_with_search(self, system: str, user: str) -> str:
        """带 web_search 工具调用;处理 pause_turn(超 10 轮服务端循环)续跑。"""
        tools = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
        messages = [{"role": "user", "content": user}]
        pt = ct = 0
        resp = None
        for _ in range(5):  # 最多续 5 次,防失控
            resp = self._client.messages.create(
                model=self._model, max_tokens=self._max_tokens,
                system=system, messages=messages, tools=tools,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None:
                pt += getattr(usage, "input_tokens", 0) or 0
                ct += getattr(usage, "output_tokens", 0) or 0
            if getattr(resp, "stop_reason", "") != "pause_turn":
                break
            messages = [{"role": "user", "content": user},
                        {"role": "assistant", "content": resp.content}]
        self.last_usage = {"prompt_tokens": pt, "completion_tokens": ct}
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def chat(self, system: str, user: str, web_search: bool = False) -> LLMResponse:
        return parse_llm_text(self._complete(system, user, web_search))

    def raw_chat(self, system: str, user: str, web_search: bool = False) -> str:
        return self._complete(system, user, web_search)


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
        max_tokens: int = 16000,
        temperature: float = 0.2,
        timeout: float = 1800.0,
    ):
        try:
            from openai import OpenAI  # type: ignore
        except ImportError as e:
            raise RuntimeError("未安装 openai 包,请 `pip install openai`") from e
        # timeout 单位秒 · 长任务(推理模型 / 大上下文)默认 10 分钟容易断
        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)
        self._base_url = base_url or ""
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        # 每次调用后填:{"prompt_tokens", "completion_tokens", 可选 "cost_usd"}
        # cost_usd 在 OpenRouter 时来自服务端返回的 usage.cost(优先),否则按 PRICE_PER_1K 估
        self.last_usage: dict[str, float] = {}

    def _web_search_extra(self) -> dict:
        """按 base_url 给出该服务的联网搜索参数;不支持则返回 {}(降级为普通调用)。"""
        base = self._base_url.lower()
        if "openrouter" in base:
            # OpenRouter:web 插件(走 Exa),对任意模型生效
            return {"extra_body": {"plugins": [{"id": "web"}]}}
        if "openai.com" in base:
            # OpenAI:仅部分模型支持;失败会被外层 try 兜底降级
            return {"extra_body": {"web_search_options": {}}}
        return {}  # DeepSeek 直连 / 国内厂商等:暂不支持,普通调用

    def _complete(self, system: str, user: str, web_search: bool = False) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        base_kwargs = dict(
            model=self._model, messages=messages,
            max_tokens=self._max_tokens, temperature=self._temperature,
        )
        extra = self._web_search_extra() if web_search else {}
        try:
            resp = self._client.chat.completions.create(**base_kwargs, **extra)
        except Exception:
            if extra:
                # 联网参数被拒(模型/服务不支持)→ 降级为普通调用,不让搜索拖垮请求
                resp = self._client.chat.completions.create(**base_kwargs)
            else:
                raise
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

    def chat(self, system: str, user: str, web_search: bool = False) -> LLMResponse:
        return parse_llm_text(self._complete(system, user, web_search))

    def raw_chat(self, system: str, user: str, web_search: bool = False) -> str:
        return self._complete(system, user, web_search)


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


def _repair_plan_json(s: str) -> str:
    """
    LLM 常见 JSON 错误就地修补:
      - 尾随逗号(`,\n}`)
      - 缺失的右花/方括号(按计数补齐)
      - 单引号 → 双引号(只对 key/字符串值;粗略)
    任何修不动的留给 json.loads 自己抛错。
    """
    # 1) 去尾随逗号:`,\n}` / `,\n]`
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    # 2) 计算花/方括号差并在末尾补齐(仅处理"少右括号",不处理"少左括号")
    open_curly = s.count("{")
    close_curly = s.count("}")
    open_brack = s.count("[")
    close_brack = s.count("]")
    if open_curly > close_curly:
        s = s + ("}" * (open_curly - close_curly))
    if open_brack > close_brack:
        s = s + ("]" * (open_brack - close_brack))
    # 再扫一次尾随逗号(补完括号后可能又有)
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    return s


def _normalize_plan_dict(d: dict) -> dict:
    """
    对解析出来的 dict 做软化处理 —— 不让 LLM 的小毛糙击穿 pydantic。
      - SkillCall.chart 如果是字符串 → 丢弃(skill 会用默认 chart)
      - SkillCall.chart 如果是 dict 但缺 x/y → 丢弃
    """
    skill_calls = d.get("skill_calls")
    if isinstance(skill_calls, list):
        for sc in skill_calls:
            if not isinstance(sc, dict):
                continue
            chart = sc.get("chart")
            if isinstance(chart, str):
                # "bar" → 丢弃(让 skill 内部决定默认图)
                sc.pop("chart", None)
            elif isinstance(chart, dict):
                if not (chart.get("x") and chart.get("y")):
                    sc.pop("chart", None)
    return d


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
    except json.JSONDecodeError:
        # 1) 尝试简单修复(尾随逗号 / 缺失右括号)
        try:
            plan_dict = json.loads(_repair_plan_json(plan_json))
        except json.JSONDecodeError:
            # 2) 上 json_repair —— 专门修 LLM 残缺 JSON
            try:
                import json_repair  # type: ignore
                plan_dict = json_repair.loads(plan_json)
            except Exception as e3:
                raise ValueError(f"computation_plan 不是有效 JSON: {e3}")
        if not isinstance(plan_dict, dict):
            raise ValueError("computation_plan 修复后不是合法 dict")

    plan_dict = _normalize_plan_dict(plan_dict)

    try:
        plan = ComputationPlan.model_validate(plan_dict)
    except Exception as e:
        # 软化失败仍兜底:再过一次更宽松的字段清理(删 chart 之类问题)
        sc_list = plan_dict.get("skill_calls") or []
        for sc in sc_list:
            if isinstance(sc, dict):
                sc.pop("chart", None)
        plan = ComputationPlan.model_validate(plan_dict)

    # ---- summary —— 多层容错,绝不因 summary 缺失而 500 ----
    summary = ""

    # 1) <summary>...</summary>(契约标准,要求闭合)
    m = _SUMMARY_TAG_RE.search(text)
    if m:
        summary = m.group(1).strip()

    # 2) 只有开标签 <summary> 没闭合 —— 模型常见疏忽
    if not summary:
        idx = text.lower().find("<summary>")
        if idx >= 0:
            tail = text[idx + len("<summary>"):]
            # 截到 </summary> 或直接到结尾
            end = tail.lower().find("</summary>")
            summary = (tail[:end] if end >= 0 else tail).strip()

    # 3) 中文【】或 markdown ## summary
    if not summary:
        m = _SUMMARY_MARK_RE.search(text)
        if m:
            summary = m.group(1).strip()

    # 4) </computation_plan> 后面的所有自由文本
    if not summary:
        idx = text.lower().find("</computation_plan>")
        if idx >= 0:
            summary = text[idx + len("</computation_plan>"):].strip()[:2000]

    # 5) 实在没有 —— 给个占位,客户端不会 500
    if not summary:
        summary = "已生成分析,详见 Excel。"

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
            max_tokens=kwargs.get("max_tokens", 16000),
            temperature=kwargs.get("temperature", 0.2),
        )
    raise ValueError(f"暂不支持的 model_type: {model_type}")
