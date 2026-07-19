"""
多 LLM 配置 + 用量统计(architecture.md §A3 扩展)。

设计:
- LLMConfig:一份"已保存"的 provider 设定(类型 + 模型 + key + base_url)
- LLMConfigStore:JSON 持久化(~/.agent-system/host-config/llm_configs.json)
- CallStat:每 (period, config_id, username) 的累计调用 / token / 花费
  period ∈ {"all", "YYYY-MM-DD"(每日), "YYYY-MM"(每月)}
- CallStatStore:三层桶(lifetime + daily + monthly)+ JSON 持久化
- ProviderManager:按 config_id 缓存 LLMProvider 实例,配置变更即失效
- discover_models:用 api_key 调 /v1/models 拉真实可用模型列表
- estimate_cost:按 PRICE_PER_1K 估算美元花费

用户表新增 llm_config_id(Optional)
LLM 路由按用户绑定的 config 选 provider(无绑定 → 返回 503 提示去配置)
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from host.llm_proxy import LLMProvider, make_provider


# ---------------------------------------------------------------------------
# Provider 预设(label / 默认 base_url / 走哪种 SDK 协议)
# 国内厂商大多兼容 OpenAI 协议,所以 "OpenAI 兼容" 这条路径承担了 80%。
# ---------------------------------------------------------------------------

PROVIDER_PRESETS: dict[str, dict[str, Any]] = {
    # 国际
    "openrouter":  {"label": "OpenRouter(聚合)",        "base_url": "https://openrouter.ai/api/v1",                       "kind": "openai_compatible"},
    "openai":      {"label": "OpenAI(官方/通用兼容)",   "base_url": "https://api.openai.com/v1",                          "kind": "openai_compatible"},
    "anthropic":   {"label": "Anthropic Claude",          "base_url": "https://api.anthropic.com/v1",                       "kind": "anthropic"},
    # 中国
    "deepseek":    {"label": "DeepSeek 深度求索(官方)", "base_url": "https://api.deepseek.com/v1",                        "kind": "openai_compatible"},
    "zhipu":       {"label": "智谱 AI(BigModel / GLM)", "base_url": "https://open.bigmodel.cn/api/paas/v4",               "kind": "openai_compatible"},
    "moonshot":    {"label": "Moonshot 月之暗面(Kimi)", "base_url": "https://api.moonshot.cn/v1",                         "kind": "openai_compatible"},
    "qwen":        {"label": "阿里百炼 / 通义千问",       "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",  "kind": "openai_compatible"},
    "doubao":      {"label": "字节豆包(火山方舟)",     "base_url": "https://ark.cn-beijing.volces.com/api/v3",           "kind": "openai_compatible"},
    "hunyuan":     {"label": "腾讯混元",                  "base_url": "https://api.hunyuan.cloud.tencent.com/v1",           "kind": "openai_compatible"},
    "baichuan":    {"label": "百川智能",                  "base_url": "https://api.baichuan-ai.com/v1",                     "kind": "openai_compatible"},
    "minimax":     {"label": "MiniMax 海螺",              "base_url": "https://api.minimax.chat/v1",                        "kind": "openai_compatible"},
    "stepfun":     {"label": "阶跃星辰 StepFun",          "base_url": "https://api.stepfun.com/v1",                         "kind": "openai_compatible"},
    "spark":       {"label": "讯飞星火",                  "base_url": "https://spark-api-open.xf-yun.com/v1",               "kind": "openai_compatible"},
    "baidu":       {"label": "百度千帆(文心一言)",     "base_url": "https://qianfan.baidubce.com/v2",                    "kind": "openai_compatible"},
    "sensetime":   {"label": "商汤 SenseNova",            "base_url": "https://api.sensenova.cn/compatible-mode/v1",        "kind": "openai_compatible"},
    "yi":          {"label": "零一万物 Yi(01.AI)",     "base_url": "https://api.lingyiwanwu.com/v1",                     "kind": "openai_compatible"},
    "siliconflow": {"label": "硅基流动 SiliconFlow",      "base_url": "https://api.siliconflow.cn/v1",                      "kind": "openai_compatible"},
    # 测试
    "stub":        {"label": "Stub(本地测试)",          "base_url": "—",                                                  "kind": "stub"},
}


# ---------------------------------------------------------------------------
# 价格表(USD / 1K tokens) —— MVP 静态表,生产可改 DB / yaml
# 数据来源:供应商官网公布价(2025-12 取价 ± 已知调整)
# 未命中模型 → 0,显示 "—"(不强行估算)
# ---------------------------------------------------------------------------

PRICE_PER_1K: dict[str, tuple[float, float]] = {
    # (input_per_1k, output_per_1k) USD
    # ---- OpenAI ----
    "gpt-4o":             (0.0025, 0.01),
    "gpt-4o-mini":        (0.00015, 0.0006),
    "gpt-4-turbo":        (0.01, 0.03),
    "gpt-5":              (0.005, 0.015),
    "gpt-5.1":            (0.005, 0.015),
    "gpt-5.1-codex":      (0.005, 0.015),
    # ---- Anthropic ----
    "claude-sonnet-4-5":  (0.003, 0.015),
    "claude-opus-4-5":    (0.015, 0.075),
    "claude-haiku-4-5":   (0.0008, 0.004),
    # ---- DeepSeek (官方 / OpenRouter) ----
    "deepseek/deepseek-v4-pro": (0.00027, 0.0011),
    "deepseek/deepseek-chat":   (0.00027, 0.0011),
    "deepseek-chat":            (0.00027, 0.0011),
    "deepseek-reasoner":        (0.00055, 0.00219),
    "deepseek-coder":           (0.00027, 0.0011),
    # ---- 智谱 GLM(¥ 折算 1USD≈7.2RMB)----
    "glm-4-plus":         (0.0069, 0.0069),
    "glm-4-flash":        (0.0000, 0.0000),  # 免费
    "glm-4-air":          (0.00014, 0.00014),
    "glm-4-airx":         (0.00139, 0.00139),
    "glm-4-long":         (0.00014, 0.00014),
    # ---- Moonshot / Kimi ----
    "moonshot-v1-8k":     (0.00167, 0.00167),
    "moonshot-v1-32k":    (0.00333, 0.00333),
    "moonshot-v1-128k":   (0.00833, 0.00833),
    # ---- Qwen / 阿里 ----
    "qwen-max":           (0.0028, 0.011),
    "qwen-plus":          (0.000111, 0.000278),
    "qwen-turbo":         (0.0000417, 0.000111),
    "qwen-long":          (0.0000694, 0.000278),
    "qwen/qwen-2.5-72b":  (0.0009, 0.0009),
    # ---- 豆包 / 火山方舟 ----
    "doubao-pro-32k":     (0.00111, 0.00278),
    "doubao-pro-128k":    (0.0069, 0.0125),
    "doubao-lite-128k":   (0.000111, 0.000139),
    # ---- 腾讯混元 ----
    "hunyuan-pro":        (0.00417, 0.0139),
    "hunyuan-standard":   (0.000625, 0.000694),
    "hunyuan-lite":       (0.0000, 0.0000),  # 免费
    # ---- 百川 ----
    "Baichuan4-Turbo":    (0.00208, 0.00208),
    "Baichuan4-Air":      (0.000139, 0.000139),
    # ---- 阶跃 ----
    "step-1-8k":          (0.00069, 0.00069),
    "step-2-16k":         (0.00528, 0.00528),
    # ---- 零一 ----
    "yi-large":           (0.00278, 0.00278),
    "yi-medium":          (0.000347, 0.000347),
    # ---- 其他常见 ----
    "meta-llama/llama-3.3-70b": (0.0007, 0.0007),
}


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """根据 PRICE_PER_1K 估算美元花费;未命中模型返回 0(显示侧用 — 表示)。"""
    # 精确匹配
    if model in PRICE_PER_1K:
        in_p, out_p = PRICE_PER_1K[model]
    else:
        # 后缀模糊(provider 前缀差异)
        suffix = model.split("/")[-1]
        if suffix in PRICE_PER_1K:
            in_p, out_p = PRICE_PER_1K[suffix]
        else:
            return 0.0
    return prompt_tokens / 1000 * in_p + completion_tokens / 1000 * out_p


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class LLMConfig:
    """一份已保存的 LLM 配置(provider 设定)。"""

    id: str
    name: str  # 展示名,如 "DeepSeek V4 Pro"
    provider_type: str  # stub / anthropic / openrouter / openai
    model_name: str
    api_key: str  # 明文存,不外露(展示走脱敏)
    base_url: str = ""  # openai/openrouter 可选;留空走默认
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["created_at"] = self.created_at.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LLMConfig":
        return cls(
            id=d["id"],
            name=d["name"],
            provider_type=d["provider_type"],
            model_name=d["model_name"],
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", "") or "",
            enabled=d.get("enabled", True),
            created_at=datetime.fromisoformat(d.get("created_at", datetime.now().isoformat())),
        )

    def masked_key(self, keep: int = 6) -> str:
        if not self.api_key:
            return "—"
        if len(self.api_key) <= keep * 2:
            return "*" * len(self.api_key)
        return f"{self.api_key[:keep]}...{self.api_key[-keep:]}"


@dataclass
class CallStat:
    config_id: str
    config_name: str
    model_name: str
    username: str
    count: int = 0
    success: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    # period:"all"(累计) / "YYYY-MM-DD"(单日) / "YYYY-MM"(单月)
    period: str = "all"


# ---------------------------------------------------------------------------
# 持久化存储
# ---------------------------------------------------------------------------


DEFAULT_STORE_PATH = Path.home() / ".agent-system" / "host-config" / "llm_configs.json"


class LLMConfigStore:
    """LLM 配置 CRUD + JSON 持久化。"""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or DEFAULT_STORE_PATH
        self._lock = threading.Lock()
        self._configs: dict[str, LLMConfig] = {}
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            from host import secret_store
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for item in data:
                item = dict(item)
                item["api_key"] = secret_store.unprotect(item.get("api_key", ""))  # 解密落盘的 key
                cfg = LLMConfig.from_dict(item)
                self._configs[cfg.id] = cfg
        except Exception:
            # 文件损坏 → 跳过(不阻塞启动)
            pass

    def _save(self) -> None:
        import logging
        from host import secret_store
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = []
        downgraded = False
        for c in self._configs.values():
            d = c.to_dict()
            raw = d.get("api_key", "")
            enc = secret_store.protect(raw)
            if raw and not secret_store.is_protected(enc):
                downgraded = True          # 加密降级(非Windows/DPAPI失败)→ key 明文落盘
            d["api_key"] = enc
            data.append(d)
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        acl_ok = secret_store.harden_file(self._path)   # 文件 ACL 收紧到仅属主可读
        if downgraded:
            logging.getLogger("clawworker").warning(
                "LLM API Key 未能加密落盘(本平台无 DPAPI 或调用失败),已明文存储 —— "
                "仅靠文件 ACL 保护,请确保 %s 权限受限", self._path)
        if not acl_ok:
            logging.getLogger("clawworker").warning(
                "LLM 配置文件 ACL 收紧失败:%s —— 同机其他用户可能可读,请手动限制权限", self._path)

    # ---- CRUD ----
    def list_all(self) -> list[LLMConfig]:
        return list(self._configs.values())

    def list_enabled(self) -> list[LLMConfig]:
        return [c for c in self._configs.values() if c.enabled]

    def get(self, config_id: str) -> Optional[LLMConfig]:
        return self._configs.get(config_id)

    def create(
        self,
        *,
        name: str,
        provider_type: str,
        model_name: str,
        api_key: str,
        base_url: str = "",
    ) -> LLMConfig:
        if not name or not model_name:
            raise ValueError("name / model_name 不能为空")
        if provider_type not in PROVIDER_PRESETS:
            raise ValueError(
                f"未知 provider_type: {provider_type}(支持:"
                f"{', '.join(PROVIDER_PRESETS.keys())})"
            )
        # 同名查重
        for c in self._configs.values():
            if c.name == name:
                raise ValueError(f"已存在同名配置「{name}」")
        with self._lock:
            cid = secrets.token_hex(6)
            while cid in self._configs:
                cid = secrets.token_hex(6)
            cfg = LLMConfig(
                id=cid,
                name=name,
                provider_type=provider_type,
                model_name=model_name,
                api_key=api_key,
                base_url=base_url,
            )
            self._configs[cid] = cfg
            self._save()
            return cfg

    def update(
        self,
        config_id: str,
        *,
        name: Optional[str] = None,
        provider_type: Optional[str] = None,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        enabled: Optional[bool] = None,
    ) -> LLMConfig:
        with self._lock:
            cfg = self._configs.get(config_id)
            if not cfg:
                raise ValueError(f"配置 {config_id} 不存在")
            if name is not None and name != cfg.name:
                for c in self._configs.values():
                    if c.id != config_id and c.name == name:
                        raise ValueError(f"已存在同名配置「{name}」")
                cfg.name = name
            if provider_type is not None:
                cfg.provider_type = provider_type
            if model_name is not None:
                cfg.model_name = model_name
            if api_key is not None and api_key != "":
                # 空字符串 = 不修改;明示传 "—" / "***" 也忽略
                if api_key not in ("***", "—"):
                    cfg.api_key = api_key
            if base_url is not None:
                cfg.base_url = base_url
            if enabled is not None:
                cfg.enabled = enabled
            self._save()
            return cfg

    def delete(self, config_id: str) -> bool:
        with self._lock:
            if config_id not in self._configs:
                return False
            self._configs.pop(config_id)
            self._save()
            return True


# ---------------------------------------------------------------------------
# 用量统计
# ---------------------------------------------------------------------------


DEFAULT_STAT_PATH = Path.home() / ".agent-system" / "host-config" / "call_stats.json"


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _month_key() -> str:
    return datetime.now().strftime("%Y-%m")


class CallStatStore:
    """
    Per (period, config_id, username) 调用统计 —— 三层桶 + JSON 持久化:
      - lifetime:累计(主机历史以来,主键 (config_id, username))
      - daily   :按天(主键 (date, config_id, username))
      - monthly :按月(主键 (month, config_id, username))

    每次 record() 同时落三个桶,然后写盘。重启后从盘上恢复。
    """

    def __init__(self, path: Optional[Path] = None):
        self._path = path or DEFAULT_STAT_PATH
        self._lock = threading.Lock()
        self._lifetime: dict[tuple[str, str], CallStat] = {}
        self._daily:    dict[tuple[str, str, str], CallStat] = {}
        self._monthly:  dict[tuple[str, str, str], CallStat] = {}
        self._load()

    # ---- 持久化 -----------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return
        for d in data.get("lifetime", []) or []:
            s = self._stat_from_dict(d, period="all")
            self._lifetime[(s.config_id, s.username)] = s
        for d in data.get("daily", []) or []:
            s = self._stat_from_dict(d, period=d.get("period", "all"))
            self._daily[(s.period, s.config_id, s.username)] = s
        for d in data.get("monthly", []) or []:
            s = self._stat_from_dict(d, period=d.get("period", "all"))
            self._monthly[(s.period, s.config_id, s.username)] = s

    @staticmethod
    def _stat_from_dict(d: dict[str, Any], period: str) -> CallStat:
        return CallStat(
            config_id=d.get("config_id", ""),
            config_name=d.get("config_name", ""),
            model_name=d.get("model_name", ""),
            username=d.get("username", ""),
            count=int(d.get("count", 0) or 0),
            success=int(d.get("success", 0) or 0),
            failed=int(d.get("failed", 0) or 0),
            prompt_tokens=int(d.get("prompt_tokens", 0) or 0),
            completion_tokens=int(d.get("completion_tokens", 0) or 0),
            cost_usd=float(d.get("cost_usd", 0.0) or 0.0),
            period=d.get("period", period) or period,
        )

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "lifetime": [asdict(s) for s in self._lifetime.values()],
                "daily":    [asdict(s) for s in self._daily.values()],
                "monthly":  [asdict(s) for s in self._monthly.values()],
            }
            # 原子写:tmp → rename
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            # 持久化失败不阻断业务
            pass

    # ---- 录入 -------------------------------------------------------------
    def record(
        self,
        *,
        config: LLMConfig,
        username: str,
        prompt_tokens: int,
        completion_tokens: int,
        success: bool,
        cost_usd: Optional[float] = None,
    ) -> None:
        if cost_usd is None:
            cost_usd = estimate_cost(config.model_name, prompt_tokens, completion_tokens)
        today = _today_key()
        month = _month_key()
        with self._lock:
            for bucket, key, period in (
                (self._lifetime, (config.id, username),         "all"),
                (self._daily,    (today, config.id, username),  today),
                (self._monthly,  (month, config.id, username),  month),
            ):
                if key not in bucket:
                    bucket[key] = CallStat(
                        config_id=config.id,
                        config_name=config.name,
                        model_name=config.model_name,
                        username=username,
                        period=period,
                    )
                s = bucket[key]
                s.count += 1
                if success:
                    s.success += 1
                else:
                    s.failed += 1
                s.prompt_tokens += prompt_tokens
                s.completion_tokens += completion_tokens
                s.cost_usd += cost_usd
                # 同步最新名字,配置重命名后报表仍能跟上
                s.config_name = config.name
                s.model_name = config.model_name
            self._save()

    # ---- 查询 -------------------------------------------------------------
    def _select(self, period: str) -> list[CallStat]:
        """根据 period 选择哪个桶的哪些条目。period:
        - "all"        → 累计桶全部
        - "today"      → 今日
        - "this_month" → 本月
        - "YYYY-MM-DD" → 指定日
        - "YYYY-MM"    → 指定月
        """
        if period == "all":
            return list(self._lifetime.values())
        if period == "today":
            today = _today_key()
            return [s for s in self._daily.values() if s.period == today]
        if period == "this_month":
            month = _month_key()
            return [s for s in self._monthly.values() if s.period == month]
        if len(period) == 10:
            return [s for s in self._daily.values() if s.period == period]
        if len(period) == 7:
            return [s for s in self._monthly.values() if s.period == period]
        return []

    def list_all(self) -> list[CallStat]:
        """向后兼容:返回累计桶全部条目。"""
        return list(self._lifetime.values())

    def by_model(self, period: str = "all") -> list[dict[str, Any]]:
        """按 config_id 聚合(忽略 username)。"""
        agg: dict[str, dict[str, Any]] = {}
        for s in self._select(period):
            key = s.config_id
            if key not in agg:
                agg[key] = {
                    "config_id": s.config_id,
                    "config_name": s.config_name,
                    "model_name": s.model_name,
                    "count": 0, "success": 0, "failed": 0,
                    "prompt_tokens": 0, "completion_tokens": 0,
                    "cost_usd": 0.0,
                }
            a = agg[key]
            a["count"] += s.count
            a["success"] += s.success
            a["failed"] += s.failed
            a["prompt_tokens"] += s.prompt_tokens
            a["completion_tokens"] += s.completion_tokens
            a["cost_usd"] += s.cost_usd
        return sorted(agg.values(), key=lambda x: x["count"], reverse=True)

    def by_user(self, period: str = "all") -> list[dict[str, Any]]:
        """按 username + config 聚合 —— 用户视角统计。"""
        rows = [
            {
                "username": s.username,
                "config_name": s.config_name,
                "model_name": s.model_name,
                "count": s.count,
                "success": s.success,
                "failed": s.failed,
                "prompt_tokens": s.prompt_tokens,
                "completion_tokens": s.completion_tokens,
                "cost_usd": s.cost_usd,
            }
            for s in self._select(period)
        ]
        return sorted(rows, key=lambda x: x["count"], reverse=True)

    def totals(self, period: str = "all") -> dict[str, Any]:
        rows = self._select(period)
        return {
            "calls":             sum(s.count for s in rows),
            "success":           sum(s.success for s in rows),
            "failed":            sum(s.failed for s in rows),
            "prompt_tokens":     sum(s.prompt_tokens for s in rows),
            "completion_tokens": sum(s.completion_tokens for s in rows),
            "total_tokens":      sum(s.prompt_tokens + s.completion_tokens for s in rows),
            "cost_usd":          sum(s.cost_usd for s in rows),
        }

    def recent_days(self, n: int = 7) -> list[dict[str, Any]]:
        """近 n 天每日汇总(供未来加趋势图用)。"""
        by_date: dict[str, dict[str, Any]] = {}
        for s in self._daily.values():
            d = by_date.setdefault(s.period, {
                "date": s.period, "calls": 0, "tokens": 0, "cost_usd": 0.0,
            })
            d["calls"] += s.count
            d["tokens"] += s.prompt_tokens + s.completion_tokens
            d["cost_usd"] += s.cost_usd
        return sorted(by_date.values(), key=lambda x: x["date"], reverse=True)[:n]

    def recent_months(self, n: int = 6) -> list[dict[str, Any]]:
        by_month: dict[str, dict[str, Any]] = {}
        for s in self._monthly.values():
            m = by_month.setdefault(s.period, {
                "month": s.period, "calls": 0, "tokens": 0, "cost_usd": 0.0,
            })
            m["calls"] += s.count
            m["tokens"] += s.prompt_tokens + s.completion_tokens
            m["cost_usd"] += s.cost_usd
        return sorted(by_month.values(), key=lambda x: x["month"], reverse=True)[:n]


# ---------------------------------------------------------------------------
# Provider 缓存 / 热加载
# ---------------------------------------------------------------------------


class ProviderManager:
    """
    按 config_id 缓存 LLMProvider 实例;配置改动后 invalidate 即可热加载。
    """

    def __init__(self, store: LLMConfigStore):
        self._store = store
        self._cache: dict[str, LLMProvider] = {}
        self._lock = threading.Lock()

    def for_config(self, config_id: str) -> Optional[LLMProvider]:
        cfg = self._store.get(config_id)
        if not cfg or not cfg.enabled:
            return None
        with self._lock:
            if config_id in self._cache:
                return self._cache[config_id]
            kwargs: dict[str, Any] = {
                "api_key": cfg.api_key,
                "model": cfg.model_name,
            }
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            try:
                prov = make_provider(cfg.provider_type, **kwargs)
            except Exception:
                return None
            self._cache[config_id] = prov
            return prov

    def invalidate(self, config_id: Optional[str] = None) -> None:
        """配置变更后调用;config_id=None 表示清空全部。"""
        with self._lock:
            if config_id is None:
                self._cache.clear()
            else:
                self._cache.pop(config_id, None)


# ---------------------------------------------------------------------------
# 模型自动探测 —— 用 api_key 调 /v1/models 拉真实列表
# ---------------------------------------------------------------------------


# 兜底 / 离线列表(无 api key、或网络失败时用)
FALLBACK_MODELS: dict[str, list[str]] = {
    "anthropic": [
        "claude-opus-4-5",
        "claude-sonnet-4-5",
        "claude-haiku-4-5",
    ],
    "openrouter": [
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-chat",
        "openai/gpt-5.1",
        "openai/gpt-5.1-codex",
        "anthropic/claude-sonnet-4-5",
        "anthropic/claude-opus-4-5",
        "qwen/qwen-2.5-72b",
        "meta-llama/llama-3.3-70b",
    ],
    "openai": [
        "gpt-5.1",
        "gpt-5.1-codex",
        "gpt-5",
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4-turbo",
    ],
    "deepseek": [
        "deepseek-chat",
        "deepseek-reasoner",
        "deepseek-coder",
    ],
    "zhipu": [
        "glm-4-plus",
        "glm-4-air",
        "glm-4-airx",
        "glm-4-flash",
        "glm-4-long",
        "glm-4v-plus",
        "glm-zero-preview",
    ],
    "moonshot": [
        "moonshot-v1-8k",
        "moonshot-v1-32k",
        "moonshot-v1-128k",
        "moonshot-v1-auto",
        "kimi-k1.5-preview",
    ],
    "qwen": [
        "qwen-max",
        "qwen-plus",
        "qwen-turbo",
        "qwen-long",
        "qwen2.5-72b-instruct",
        "qwen2.5-32b-instruct",
        "qwen2.5-14b-instruct",
        "qwen-coder-plus",
    ],
    "doubao": [
        "doubao-pro-256k",
        "doubao-pro-128k",
        "doubao-pro-32k",
        "doubao-pro-4k",
        "doubao-lite-128k",
        "doubao-1-5-pro-256k",
    ],
    "hunyuan": [
        "hunyuan-pro",
        "hunyuan-turbo",
        "hunyuan-large",
        "hunyuan-standard",
        "hunyuan-lite",
        "hunyuan-vision",
    ],
    "baichuan": [
        "Baichuan4-Turbo",
        "Baichuan4-Air",
        "Baichuan4",
        "Baichuan3-Turbo",
        "Baichuan3-Turbo-128k",
    ],
    "minimax": [
        "abab6.5s-chat",
        "abab6.5g-chat",
        "abab6.5t-chat",
        "abab5.5-chat",
        "MiniMax-Text-01",
    ],
    "stepfun": [
        "step-2-16k",
        "step-1-8k",
        "step-1-32k",
        "step-1-128k",
        "step-1-256k",
        "step-1-flash",
    ],
    "spark": [
        "4.0Ultra",
        "generalv3.5",
        "generalv3",
        "general",
        "pro-128k",
    ],
    "baidu": [
        "ernie-4.0-turbo-8k",
        "ernie-4.0-8k",
        "ernie-3.5-8k",
        "ernie-speed-128k",
        "ernie-lite-8k",
    ],
    "sensetime": [
        "SenseChat-5",
        "SenseChat-Turbo",
        "SenseChat-128K",
        "SenseChat-32K",
    ],
    "yi": [
        "yi-large",
        "yi-medium",
        "yi-large-turbo",
        "yi-spark",
        "yi-vision",
    ],
    "siliconflow": [
        "deepseek-ai/DeepSeek-V3",
        "Qwen/Qwen2.5-72B-Instruct",
        "meta-llama/Llama-3.3-70B-Instruct",
        "01-ai/Yi-1.5-34B-Chat-16K",
        "THUDM/glm-4-9b-chat",
    ],
    "stub": ["stub"],
}


def discover_models(
    provider_type: str,
    api_key: str,
    base_url: str = "",
    timeout: float = 8.0,
) -> tuple[list[str], str]:
    """
    用 api_key 拉真实可用模型列表;失败回退到 FALLBACK_MODELS。
    OpenAI 兼容协议的(国内大多数厂商)统一走 /models 端点。

    Returns:
        (models, source)
        source 形如:
          - "api"                         实时拉取成功
          - "fallback:no_provider"        provider 未知
          - "fallback:stub_provider"      stub provider 没有真实端点
          - "fallback:no_api_key"         没传 api_key
          - "fallback:http_<code>"        HTTP 错误(401/403/429/500…)
          - "fallback:net_<exc>"          网络异常(timeout / DNS / connreset…)
          - "fallback:empty_response"     端点 200 但 data 为空
    """
    preset = PROVIDER_PRESETS.get(provider_type)
    fallback = FALLBACK_MODELS.get(provider_type, [])

    if not preset:
        return fallback, "fallback:no_provider"
    if preset["kind"] == "stub":
        return fallback, "fallback:stub_provider"
    if not api_key:
        return fallback, "fallback:no_api_key"

    effective_base = (base_url or preset["base_url"]).rstrip("/")

    try:
        import urllib.error
        import urllib.request

        if preset["kind"] == "anthropic":
            url = effective_base + "/models"
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            }
        else:
            # openai_compatible — 涵盖 openai / openrouter / 所有国内兼容厂商
            url = effective_base + "/models"
            headers = {"Authorization": f"Bearer {api_key}"}

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return fallback, f"fallback:http_{e.code}"
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            return fallback, f"fallback:net_{type(reason).__name__}"
        except Exception as e:  # socket.timeout, ssl errors 等
            return fallback, f"fallback:net_{type(e).__name__}"

        try:
            data = json.loads(raw)
        except Exception:
            return fallback, "fallback:invalid_json"

        items = data.get("data") or data.get("models") or []
        models: list[str] = []
        for m in items:
            if isinstance(m, str):
                models.append(m)
            elif isinstance(m, dict):
                mid = m.get("id") or m.get("name") or m.get("model")
                if mid:
                    models.append(mid)
        if not models:
            return fallback, "fallback:empty_response"
        models.sort()
        return models, "api"
    except Exception as e:
        return fallback, f"fallback:exc_{type(e).__name__}"
