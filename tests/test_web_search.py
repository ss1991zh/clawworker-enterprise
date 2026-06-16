"""
联网搜索接线回归测试 —— 不打真实网络,用假 OpenAI/Anthropic 客户端验证:
  · web_search=True 时 OpenRouter 走 plugins:[{id:web}]、OpenAI 走 web_search_options、
    其它 provider 不带联网参数(降级)
  · 联网参数被拒(不支持的模型/服务)→ 自动降级为普通调用
  · web_search=False 时绝不带联网参数
"""
from __future__ import annotations

import pytest

from host.llm_proxy import OpenAICompatibleProvider


class _FakeCompletions:
    def __init__(self, calls, fail_on_extra=False):
        self.calls = calls
        self.fail_on_extra = fail_on_extra

    def create(self, **kwargs):
        self.calls.append(kwargs)
        # 模拟"模型不支持联网参数"→ 首次带 extra 的调用抛错
        if self.fail_on_extra and ("extra_body" in kwargs):
            raise RuntimeError("web search not supported by this model")

        class _Msg:
            content = "ok"

        class _Choice:
            message = _Msg()

        class _Resp:
            choices = [_Choice()]
            usage = type("U", (), {"prompt_tokens": 3, "completion_tokens": 5, "cost": None})()
        return _Resp()


def _provider(base_url, fail_on_extra=False):
    p = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
    p._base_url = base_url
    p._model = "test-model"
    p._max_tokens = 100
    p._temperature = 0.2
    p.last_usage = {}
    calls: list = []
    p._client = type("C", (), {"chat": type("Ch", (), {"completions": _FakeCompletions(calls, fail_on_extra)})()})()
    return p, calls


def test_openrouter_uses_web_plugin():
    p, calls = _provider("https://openrouter.ai/api/v1")
    p.raw_chat("sys", "今天北京天气?", web_search=True)
    plugins = calls[-1].get("extra_body", {}).get("plugins")
    assert plugins and plugins[0]["id"] == "web"
    assert plugins[0].get("max_results") == 10        # 多拉源
    assert plugins[0].get("search_prompt")            # 有搜索引导


def test_openai_uses_web_search_options():
    p, calls = _provider("https://api.openai.com/v1")
    p.raw_chat("sys", "q", web_search=True)
    assert calls[-1].get("extra_body") == {"web_search_options": {}}


def test_other_provider_no_web_param():
    # DeepSeek 直连等:不支持 → 不带任何联网参数
    p, calls = _provider("https://api.deepseek.com/v1")
    p.raw_chat("sys", "q", web_search=True)
    assert "extra_body" not in calls[-1]


def test_web_search_off_never_sends_param():
    p, calls = _provider("https://openrouter.ai/api/v1")
    p.raw_chat("sys", "q", web_search=False)
    assert "extra_body" not in calls[-1]


def test_unsupported_model_falls_back_to_plain():
    # 带 extra 的调用被拒 → 自动重试普通调用,不让搜索拖垮请求
    p, calls = _provider("https://api.openai.com/v1", fail_on_extra=True)
    out = p.raw_chat("sys", "q", web_search=True)
    assert out == "ok"
    assert len(calls) == 2                       # 第一次带 extra 失败,第二次降级
    assert "extra_body" not in calls[-1]         # 降级调用不带联网参数
