"""
v2 客户端分析 pipeline — 无 LangGraph,单层函数。

流程:
  ① ensure_cipher(在消息附件或追问)
  ② load schema sidecar
  ③ load metadata sidecar
  ④ 调 host /llm/chat 拿 plan(SkillCall 列表)
  ⑤ ps.read_excel / read_csv 加载 cipher → CipherDataFrame
  ⑥ for sc in plan.skill_calls: run_skill → (sheet_name, df, chart)
  ⑦ writer.write_skill_results → Excel
  ⑧ B6-3 summary 零明文过滤 → 返回友好 summary

每一步都通过 step_callback 实时上报给前端,前端看到一行一行的"进度"。
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from client import skills_loader
from client.tools.runtime import Runtime
from client.tools.skills import run_skill, SKILLS
from client.webui import codegen as codegen_mod
from client.webui.plan_validator import validate_and_repair_plan
from client.webui.writer import (
    derive_excel_stem,
    export_cipher_as_is,
    export_skill_results_encrypted,
    write_skill_results,
)
from client.permissions import scan_summary
from shared.contract import ComputationPlan


StepCallback = Callable[[str, str], None]  # (kind, label)
# kind: think | call | result | error
# label: 一行简短描述,前端直接显示

# should_cancel: () -> bool;若返回 True,pipeline 在下一个检查点抛 CancelledError
ShouldCancel = Callable[[], bool]


# —— 本次运行的 token 用量累计(线程本地;ask() 入口重置、出口汇总到 result["tokens"])——
# host 在 /llm/chat、/llm/freechat 响应里回带 usage;每个 LLM 助手把它累加进来。
_usage_tls = threading.local()


def _usage_reset() -> None:
    _usage_tls.total = 0


def _usage_add(u: Any) -> None:
    if not isinstance(u, dict):
        return
    try:
        n = u.get("total_tokens")
        if n is None:
            n = int(u.get("prompt_tokens", 0) or 0) + int(u.get("completion_tokens", 0) or 0)
        _usage_tls.total = int(getattr(_usage_tls, "total", 0) or 0) + int(n or 0)
    except (TypeError, ValueError):
        pass


def _usage_total() -> int:
    return int(getattr(_usage_tls, "total", 0) or 0)


class CancelledError(Exception):
    """pipeline.ask 被用户取消时抛。"""


def _post_cancellable(
    url: str,
    *,
    headers: dict,
    json_body: dict,
    timeout: float,
    should_cancel: Optional[Callable[[], bool]] = None,
    poll_interval: float = 0.2,
) -> httpx.Response:
    """
    httpx.post 包一层:把请求丢子线程跑,主线程 200ms 轮询 cancel。
    用户点停止 → 不等 LLM 返回直接抛 CancelledError,孤儿线程会自己结束被回收。
    """
    chk = should_cancel or (lambda: False)
    box: dict = {}

    def worker():
        try:
            box["resp"] = httpx.post(
                url, headers=headers, json=json_body,
                # 整体允许 timeout 秒,但 read/write/pool 都不主动断 —— 长任务靠 cancel
                timeout=httpx.Timeout(timeout, connect=10.0, read=None, write=None, pool=None),
                trust_env=False,   # 局域网连主机不走系统代理(Clash 等会劫持返回空 502)
            )
        except Exception as e:
            box["err"] = e

    t = threading.Thread(target=worker, daemon=True, name="llm-post")
    t.start()
    while t.is_alive():
        if chk():
            # 不 join,直接抛 —— 线程会在自己的 httpx 调用结束时自然释放
            raise CancelledError("用户已停止")
        t.join(timeout=poll_interval)
    if "err" in box:
        raise box["err"]
    return box["resp"]


# ----------------------------------------------------------------------------
# LLM 拿 plan
# ----------------------------------------------------------------------------

# 优先匹配 <computation_plan>(契约),兜底匹配 markdown json fence
_PLAN_TAG_RE = re.compile(r"<computation_plan>\s*(\{.*?\})\s*</computation_plan>", re.DOTALL)
_PLAN_FENCED_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_SUMMARY_TAG_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)


def _extract_plan_and_summary(text: str) -> tuple[ComputationPlan, str]:
    """从 LLM raw 文本里提 plan + summary。容错 3 层。"""
    if not text or not text.strip():
        raise ValueError("LLM 返回空文本(可能 max_tokens 用光)")

    # plan
    m = _PLAN_TAG_RE.search(text)
    if not m:
        m = _PLAN_FENCED_RE.search(text)
    if not m:
        raise ValueError("LLM 响应没找到 <computation_plan> 或 ```json``` 块")
    try:
        plan_dict = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        raise ValueError(f"computation_plan 不是合法 JSON:{e}")

    plan = ComputationPlan.model_validate(plan_dict)

    # summary — 容错:没 <summary> 标签也不算致命错误
    sm = _SUMMARY_TAG_RE.search(text)
    summary = sm.group(1).strip() if sm else ""
    if not summary:
        # 取 </computation_plan> 之后的所有文字做 summary
        after = text.split("</computation_plan>", 1)
        if len(after) == 2:
            summary = after[1].strip()[:500]
        if not summary:
            summary = "已生成分析,详见 Excel。"

    return plan, summary


# ----------------------------------------------------------------------------
# 意图识别 —— 区分"自由聊天"和"加密数据分析"
# ----------------------------------------------------------------------------

_ANALYSIS_KEYWORDS = (
    # 中文动词 / 名词
    "统计", "计算", "算一下", "算下", "算出", "算算", "分析", "汇总",
    "排名", "排行", "明细", "对比", "占比",
    "完成率", "回款率", "毛利率", "比率", "比例",
    "平均", "均值", "总和", "求和", "总计", "合计",
    "按", "分组", "分布", "分类", "描述", "概览", "概述",
    "趋势", "预测", "环比", "同比",
    "看每", "看各", "每位", "每个", "每人", "每月", "每天",
    "Excel", "表格", "导出", "出表",
    # 英文
    "sum ", "mean ", "average", "count(", "group by", "groupby",
    "analyze", "analyse", "stats", "top", "bottom", "rank",
)


# "知识/概念提问" 标记 —— 问某方法/概念是什么、怎么算、口径/公式,而非要对数据做计算。
# 命中这些(且无下面的"对数据操作"标记)→ 应走自由聊天 / 联网,不做密态分析。
_KNOWLEDGE_Q_MARKERS = (
    "是什么", "什么是", "啥是", "是啥", "什么意思", "啥意思", "何为",
    "怎么算", "怎样算", "如何算", "怎么计算", "怎样计算", "如何计算",
    "计算口径", "计算方式", "计算方法", "计算公式", "的公式", "公式是",
    "定义", "含义", "概念", "原理", "区别", "介绍一下", "介绍下",
    "解释一下", "解释下", "科普", "为什么", "怎么理解", "适用于什么",
    "什么场景", "有哪些方法", "怎么做", "如何做",
)
# "对这份数据操作"的强标记 —— 出现则即便像知识问题也按分析处理(在数据上算)。
_DATA_OP_MARKERS = (
    "这份", "这个表", "这张表", "这些数据", "表里", "数据中", "数据里",
    "上传", "附件", "文件里", "文件中", "每个人", "每位", "每人", "各位",
    "top", "排名", "排行", "导出", "出表", "生成excel", "生成 excel",
)


def _looks_like_knowledge_question(user_query: str) -> bool:
    """问概念/口径/公式(而非要在用户数据上计算)→ True。"""
    if not user_query:
        return False
    q = user_query.lower()
    if any(m.lower() in q for m in _DATA_OP_MARKERS):
        return False  # 明确要对数据操作 → 不是纯知识问题
    return any(m.lower() in q for m in _KNOWLEDGE_Q_MARKERS)


# "实时 / 联网查询"标记 —— 查天气、新闻、行情等外部实时信息,与"分析用户数据"无关。
# 命中(且没有"对这份数据操作"标记)→ 走自由聊天 / 联网,即便会话里沿用着旧密文。
_WEB_LOOKUP_MARKERS = (
    "天气", "气温", "下雨", "下雪", "降雨", "台风", "空气质量", "雾霾",
    "新闻", "最新消息", "热点", "头条", "实时", "股价", "股市", "大盘", "指数",
    "汇率", "油价", "金价", "票价", "机票", "比分", "赛事", "赛程", "票房",
    "上映", "几点开", "现在几点", "今天几号", "今天是", "明天", "后天", "近期",
    "搜索", "查询", "查找", "查一下", "搜一下", "查查", "搜搜",
    "百度", "谷歌", "google", "上网查", "联网",
)


def _looks_like_web_lookup(user_query: str) -> bool:
    """查外部实时信息(天气/新闻/行情等),而非对用户数据计算 → True。"""
    if not user_query:
        return False
    q = user_query.lower()
    if any(m.lower() in q for m in _DATA_OP_MARKERS):
        return False  # 明确要对这份数据操作 → 不是外部查询
    return any(m.lower() in q for m in _WEB_LOOKUP_MARKERS)


# 排程意图:必须出现"重复周期"线索,才认为用户想建定时任务(避免误判普通分析)
_SCHEDULE_MARKERS = (
    "每天", "每日", "每周", "每星期", "每月", "每个月", "每隔", "每小时",
    "工作日", "周末", "双休", "定时", "定期", "自动跑", "自动运行", "按时",
    "每分钟", "每天早上", "每天晚上", "每周一", "每周五",
)
# 明确"创建任务"的强信号(即使没有完整周期也触发)
_TASK_INTENT_MARKERS = ("定时任务", "创建任务", "建个任务", "设个任务", "设定任务", "schedule", "cron")


def looks_like_schedule_request(user_query: str) -> bool:
    """普通会话里是否在表达「创建定时任务」的意图。
    显式"定时任务"字样直接判真;否则需要"重复周期"线索 **且** 有具体时刻或强周期信号,
    以免把「每天的销售趋势」这类普通分析误判成建任务。纯知识问题排除。"""
    if not user_query:
        return False
    q = user_query.strip()
    if any(m in q for m in _TASK_INTENT_MARKERS):
        return True
    if _looks_like_knowledge_question(q):
        return False
    if not any(m in q for m in _SCHEDULE_MARKERS):
        return False
    # 具体时刻(9点 / 09:00 / 早上…)
    has_clock = bool(re.search(r"\d{1,2}\s*[点:：]", q)) or \
        any(w in q for w in ("早上", "早晨", "上午", "中午", "下午", "晚上", "傍晚", "凌晨", "夜里"))
    # 强周期信号(工作日 / 每周X / 每月N号 / 每隔 / 每小时 …)
    strong = (any(w in q for w in ("工作日", "周末", "双休", "每周", "每星期", "每月",
                                   "每个月", "每隔", "每小时", "每分钟"))
              or bool(re.search(r"(?:周|星期)[一二三四五六日天]", q))
              or bool(re.search(r"\d{1,2}\s*[号日]", q)))
    return has_clock or strong


_TASK_EXTRACT_SYSTEM = (
    "你是定时任务配置助手。用户用一句话描述了想定期自动执行的数据分析。"
    "请从中抽取结构化字段,**只输出一个 JSON 对象**,不要任何解释或代码块标记。\n"
    "字段:\n"
    '  "name": 简短任务名(8 字内,概括要做的事,如「每日回款率」),\n'
    '  "question": 要执行的分析问题(完整、可直接拿去算,如「按大区统计本月回款率 TOP10」),\n'
    '  "schedule_text": 排程的中文原话(如「每天早上9点」「每周一」「每月1号」;没提到则空串),\n'
    '  "needs_data": 是否需要对用户的数据文件计算(true/false;只是问概念/闲聊才 false)。\n'
    "缺失的字段给空串或合理默认。只输出 JSON。"
)


def extract_task_slots(host_url: str, token: str, text: str) -> dict:
    """调 LLM 从一句话里抽取定时任务槽位;再用本地解析把 schedule_text 转 cron。
    返回 {name, question, schedule_text, cron, cron_readable, needs_data, missing[]}。"""
    import json as _json

    slots = {"name": "", "question": text.strip(), "schedule_text": "", "needs_data": True}
    try:
        raw = call_llm_for_freechat(
            host_url, token,
            f"{_TASK_EXTRACT_SYSTEM}\n\n用户描述:{text.strip()}",
            history=None, should_cancel=None, web_search=False,
        )
        m = re.search(r"\{.*\}", raw or "", re.DOTALL)
        if m:
            parsed = _json.loads(m.group(0))
            for k in ("name", "question", "schedule_text"):
                if isinstance(parsed.get(k), str) and parsed[k].strip():
                    slots[k] = parsed[k].strip()
            if isinstance(parsed.get("needs_data"), bool):
                slots["needs_data"] = parsed["needs_data"]
    except Exception:
        pass

    # schedule_text → cron(复用本地自然语言解析器)
    cron, cron_readable = "", ""
    sched_src = slots["schedule_text"] or text
    try:
        from client.webui.scheduler import parse_natural_schedule
        pr = parse_natural_schedule(sched_src)
        if pr.get("ok"):
            cron = pr.get("cron", "")
            cron_readable = pr.get("readable", "")
    except Exception:
        pass
    slots["cron"] = cron
    slots["cron_readable"] = cron_readable

    missing = []
    if not slots["question"]:
        missing.append("question")
    if not cron:
        missing.append("schedule")
    slots["missing"] = missing
    return slots


def detect_intent_ambiguity(user_query: str, has_attachment: bool) -> Optional[dict]:
    """检测"矛盾/不确定"的意图,需要先让用户澄清。返回澄清规格或 None。

    通用框架:每个检测器命中就返回 {question, options:[{label, action}], allow_free}。
    action ∈ wizard(创建定时任务)/ analyze(只算当前数据一次)/ freechat / free(自己说)。
    目前规则:
      · 排程词(每天/每周…)+ 同时带了附件 → 定时处理 vs 只算这个附件,二选一。
    以后可在此追加更多歧义规则。
    """
    if not user_query:
        return None
    # 规则一:既像"定时任务"又带了附件 —— 到底是定时跑、还是只算这次的附件?
    if has_attachment and looks_like_schedule_request(user_query):
        return {
            "kind": "schedule_vs_oneshot",
            "question": "你的描述里既有「定时」的意思,又带了一个附件 —— 这两种做法不一样,你想要哪种?",
            "options": [
                {"label": "创建定时任务,按计划自动处理(附件只作示例 / 之后按文件夹取最新)", "action": "wizard"},
                {"label": "只分析当前这个附件一次(忽略「定时」)", "action": "analyze"},
            ],
            "allow_free": True,
        }
    return None


def looks_like_analysis(user_query: str) -> bool:
    """启发式:是否像数据分析意图。否 → 走自由聊天端点。"""
    if not user_query:
        return False
    # 知识/概念提问、或查外部实时信息(天气/新闻/行情)→ 判为"非分析",走自由聊天/联网
    # (即便会话里沿用着旧密文,也不会把这类问题误拉进数据分析)
    if _looks_like_knowledge_question(user_query) or _looks_like_web_lookup(user_query):
        return False
    q = user_query.lower()
    for kw in _ANALYSIS_KEYWORDS:
        if kw.lower() in q:
            return True
    # 很长的问题通常也意味着复杂的分析意图
    if len(user_query) > 60:
        return True
    return False


# ----------------------------------------------------------------------------
# 自由聊天 —— /llm/freechat
# ----------------------------------------------------------------------------

_FREECHAT_SYSTEM = (
    "你是 Clawworker 企业版 · 同态加密数据分析助手。"
    "用户当前的问题不是要对他的数据出报表,而是闲聊或**概念/方法咨询**"
    "(如「RFM 怎么算」「目标完成率的口径是什么」「这种指标适用什么场景」)。"
    "请用简洁、专业的中文**直接回答问题本身**(讲清定义 / 计算口径 / 公式 / 适用场景);"
    "若已开启联网搜索,优先采用查到的最新、权威信息,并简述要点。"
    "**引用来源链接统一放在相关句子的句号(。)之后**,不要把链接插在句子中间打断阅读;"
    "同一处有多个链接时,链接之间用「 · 」分隔。"
    "不要输出 <computation_plan> 或 JSON 块,也不要假设你看过用户的数据。"
    "仅当用户确实想对某份数据出表时,才提醒:点下方回形针选一份已加密文件,"
    "再问「按大区统计完成率」「TOP10 销售」这类问题即可生成 Excel。"
)


def call_llm_for_freechat(
    host_url: str, token: str, user_query: str,
    history: Optional[list[dict]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    timeout: float = 1800.0,
    web_search: bool = False,
) -> str:
    """调 host /llm/freechat,返回纯文本回复。history 可选透传;web_search 可选联网搜索。"""
    r = _post_cancellable(
        f"{host_url}/llm/freechat",
        headers={"Authorization": f"Bearer {token}"},
        json_body={
            "system": _FREECHAT_SYSTEM,
            "user": user_query,
            "history": history or [],
            "web_search": bool(web_search),
        },
        timeout=timeout,
        should_cancel=should_cancel,
    )
    if r.status_code == 401:
        raise PermissionError("登录已过期")
    r.raise_for_status()
    body = r.json()
    _usage_add(body.get("usage"))
    return body.get("text", "") or "(LLM 返回空文本)"


def call_llm_for_plan_repair(
    host_url: str, token: str, system_prompt: str, original_query: str, schema: dict,
    prev_plan: ComputationPlan, warnings: list[str],
    history: Optional[list[dict]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    timeout: float = 1800.0,
) -> tuple[ComputationPlan, str]:
    """
    LLM 回环修正 —— validator 检出的 warning 反馈给 LLM,要它带着上下文重出 plan。
    场景:LLM 漏写 compute / 用了校验表里没的指标名 / num_col 字段对不上 等。
    """
    warn_lines = "\n".join(
        f"  · {w[len('warn:'):].strip()}"
        for w in warnings if w.startswith("warn:")
    )
    prev_plan_json = json.dumps(prev_plan.model_dump(), ensure_ascii=False, indent=2)
    user_msg = (
        f"用户原问题:\n{original_query}\n\n"
        f"你刚才生成的 plan(未通过业务校验):\n"
        f"```json\n{prev_plan_json}\n```\n\n"
        f"**校验未修复项**:\n{warn_lines}\n\n"
        f"数据 schema:\n{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"请按 system prompt 的「派生指标识别铁律」**完整重写一份 plan**,务必:\n"
        f"  1. sheet_name / value_cols / sort_by 里出现的 X率 / X比例 / X占比 / X差 / X贡献\n"
        f"     **必须在 compute 里有同名条目**(可用 op:div / sub / add / mul / formula)\n"
        f"  2. ratio_by_group 必须有有效 num_col / den_col,字段严格取自 schema\n"
        f"  3. 字段名直接复用 schema(含括号、单位都不要省)\n"
        f"  4. 若 schema 缺关键字段,**改用 describe 兜底 + summary 说明缺什么**,别瞎编公式\n"
        f"只输出 <computation_plan>...</computation_plan> + <summary>...</summary> 两段,不要解释。"
    )
    r = _post_cancellable(
        f"{host_url}/llm/chat",
        headers={"Authorization": f"Bearer {token}"},
        json_body={
            "system": system_prompt, "user": user_msg,
            "history": history or [],
        },
        timeout=timeout,
        should_cancel=should_cancel,
    )
    if r.status_code == 401:
        raise PermissionError("登录已过期")
    r.raise_for_status()
    body = r.json()
    _usage_add(body.get("usage"))
    if "computation_plan" in body and "summary" in body:
        plan = ComputationPlan.model_validate(body["computation_plan"])
        return plan, body["summary"]
    raise ValueError(f"repair LLM 返回未知格式: {list(body.keys())[:5]}")


def call_llm_for_codegen(
    host_url: str, token: str, system: str, user: str,
    history: Optional[list[dict]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    timeout: float = 1800.0,
    web_search: bool = False,
) -> str:
    """调 host /llm/freechat 拿原始文本(含 ```python``` 代码块 + summary)。web_search 可选联网。"""
    r = _post_cancellable(
        f"{host_url}/llm/freechat",
        headers={"Authorization": f"Bearer {token}"},
        json_body={"system": system, "user": user, "history": history or [],
                   "web_search": bool(web_search)},
        timeout=timeout,
        should_cancel=should_cancel,
    )
    if r.status_code == 401:
        raise PermissionError("登录已过期")
    r.raise_for_status()
    body = r.json()
    _usage_add(body.get("usage"))
    return body.get("text", "") or ""


def call_llm_for_plan(
    host_url: str, token: str, system_prompt: str, user_query: str, schema: dict,
    history: Optional[list[dict]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    timeout: float = 1800.0,
) -> tuple[ComputationPlan, str]:
    """调 host /llm/chat,返回 (plan, summary)。"""
    user_msg = (
        f"用户问题:\n{user_query}\n\n"
        f"数据 schema(只有字段名,没有明文数据):\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"请按 system prompt 输出 computation_plan + summary。"
    )
    r = _post_cancellable(
        f"{host_url}/llm/chat",
        headers={"Authorization": f"Bearer {token}"},
        json_body={
            "system": system_prompt, "user": user_msg,
            "history": history or [],
        },
        timeout=timeout,
        should_cancel=should_cancel,
    )
    if r.status_code == 401:
        raise PermissionError("登录已过期")
    r.raise_for_status()
    body = r.json()
    _usage_add(body.get("usage"))
    # 老 /llm/chat 已经返回 parse 过的 {computation_plan, summary},但我们 parse_llm_text
    # 在 host 端可能拒掉新格式;直接再 parse 一次也能兼容
    if "computation_plan" in body and "summary" in body:
        plan = ComputationPlan.model_validate(body["computation_plan"])
        return plan, body["summary"]
    raise ValueError(f"主机返回未知格式: {list(body.keys())[:5]}")


# ----------------------------------------------------------------------------
# 加载密文 → CipherDataFrame
# ----------------------------------------------------------------------------

def load_cipher_df(cipher_path: Path):
    """ps.read_excel(index_col=0) / ps.read_csv 加载密文文件。"""
    Runtime.get().ensure_all_initialized()
    import pandaseal as ps  # noqa: F401(初始化副作用)

    suffix = cipher_path.suffix.lower()
    if suffix == ".csv":
        return ps.read_csv(str(cipher_path))
    if suffix in (".xlsx", ".xls"):
        try:
            return ps.read_excel(str(cipher_path), index_col=0)
        except Exception:
            return ps.read_excel(str(cipher_path))
    if suffix == ".json":
        return ps.read_json(str(cipher_path))
    raise ValueError(f"pandaseal 不支持文件类型: {suffix}")


# ----------------------------------------------------------------------------
# 加载 metadata + schema sidecar
# ----------------------------------------------------------------------------

def load_metadata(cipher_path: Path) -> tuple[list[dict], list[str]]:
    """从 cipher 旁挂 *.meta.csv 加载 metadata_rows + metadata_columns。"""
    meta_p = cipher_path.with_suffix(cipher_path.suffix + ".meta.csv")
    if not meta_p.exists():
        return [], []
    try:
        import pandas as pd
        df = pd.read_csv(meta_p)
        return df.to_dict("records"), list(df.columns)
    except Exception:
        return [], []


def load_schema(cipher_path: Path) -> dict:
    """从 cipher 旁挂 *.schema.json 加载 schema 推断。"""
    schema_p = cipher_path.with_suffix(cipher_path.suffix + ".schema.json")
    if not schema_p.exists():
        return {}
    try:
        return json.loads(schema_p.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ----------------------------------------------------------------------------
# 主入口:跑一次完整分析
# ----------------------------------------------------------------------------


def _fold_text_attachments(user_query: str, atts: Optional[list[dict]]) -> str:
    """把 text_attachments 折到 user_query 前面 —— LLM 看到时已经合并好。"""
    if not atts:
        return user_query
    parts: list[str] = []
    for a in atts:
        nm = a.get("name") or "attachment"
        content = (a.get("content") or "").strip()
        if not content:
            continue
        parts.append(f"[附件文件 · {nm}]\n{content}")
    parts.append("[用户问题]")
    parts.append(user_query)
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# 定时任务代码固化缓存 —— 首次运行成功后固化生成代码,之后每次到点复用同一份,
# 保证同一任务每次运行的输出结构完全一致(不再"每次现写、次次不同")。
# 任务问题或数据 schema(列名)变化 → 签名失配 → 自动重新生成。
# ---------------------------------------------------------------------------

_CODEGEN_CACHE_DIR = Path.home() / ".agent-system" / "scheduler" / "codegen_cache"


def _codegen_cache_sig(effective_query: str, schema: dict) -> str:
    import hashlib
    cols = ",".join(sorted(
        str(c.get("name", "")) for c in (schema or {}).get("columns", [])
    ))
    return hashlib.sha256(f"{effective_query}|{cols}".encode("utf-8")).hexdigest()[:16]


def _codegen_cache_load(cache_key: str, sig: str) -> Optional[dict]:
    """命中返回 {code, summary, lazy_waived};签名失配或无缓存返回 None。"""
    import json
    f = _CODEGEN_CACHE_DIR / f"{cache_key}.json"
    try:
        if not f.exists():
            return None
        data = json.loads(f.read_text(encoding="utf-8"))
        if data.get("sig") != sig or not data.get("code"):
            return None
        return {"code": data["code"], "summary": data.get("summary") or "",
                "lazy_waived": bool(data.get("lazy_waived"))}
    except Exception:
        return None


def _codegen_cache_save(cache_key: str, sig: str, code: str, summary: str,
                        lazy_waived: bool = False) -> None:
    import json
    try:
        _CODEGEN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (_CODEGEN_CACHE_DIR / f"{cache_key}.json").write_text(
            json.dumps({"sig": sig, "code": code, "summary": summary,
                        "lazy_waived": lazy_waived},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _codegen_cache_delete(cache_key: str) -> None:
    try:
        (_CODEGEN_CACHE_DIR / f"{cache_key}.json").unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# LLM 偷懒检测 —— 用户要全量,生成代码却 head()/sample() 截断
# ---------------------------------------------------------------------------

_FULL_QUERY_PAT = re.compile(r"所有|全部|每个|每位|每人|全员|全体|逐[行条]|明细|\d{2,}\s*[个条人位]")
_TRUNCATION_PAT = re.compile(
    r"\.head\s*\(|\.tail\s*\(|\.sample\s*\(|\.nlargest\s*\(|\.nsmallest\s*\("
    r"|\.iloc\s*\[\s*\d*\s*:\s*\d+"          # .iloc[:20] / .iloc[0:20]
    r"|\.loc\s*\[\s*:\s*\d+"                  # .loc[:20]
    r"|\[\s*:\s*\d{1,3}\s*\]"                 # full[:20] 裸切片
)
_TOPN_QUERY_PAT = re.compile(r"top|前\s*\d+|最[高低大小].{0,3}\d+|排名前|倒数", re.IGNORECASE)


def _detect_lazy_truncation(query: str, code: str) -> bool:
    """用户要全量(且没要 TOP-N)而代码里有截断 → 判定偷懒。"""
    if not _FULL_QUERY_PAT.search(query or ""):
        return False
    if _TOPN_QUERY_PAT.search(query or ""):
        return False
    return bool(_TRUNCATION_PAT.search(code or ""))


# 按行序计算的窗口/时序函数 —— 未排序就调用会算出"数值全错但不报错"的结果
_WINDOW_FUNC_PAT = re.compile(
    r"\.(?:diff|shift|pct_change|cumsum|cumprod|cummax|cummin|rolling|expanding|ewm)\s*\(|"
    r"window\.(?:diff|lag|rolling_mean|rolling|pct_change|cumsum)\s*\("
)
_SORT_PAT = re.compile(r"\.sort_values\s*\(|\.sort_index\s*\(|window\.[A-Za-z_]+\([^)]*sort")


def _detect_unsorted_window_risk(code: str) -> bool:
    """
    代码用了按行序计算的窗口函数(环比/移动平均/累计…)但**全篇没有任何排序调用**
    → 高风险:结果可能数值全错却不报错。返回 True 表示需回喂 LLM 修正 + 禁止固化缓存。
    这是静态启发式(宁可多问一次,不让错误代码被固化后每天复用)。
    """
    c = code or ""
    if not _WINDOW_FUNC_PAT.search(c):
        return False
    return not _SORT_PAT.search(c)


# 筛选类问题(找异常/超期/不达标…)合法输出子集,不做全量验收
_FILTER_QUERY_PAT = re.compile(
    r"异常|离群|超过|低于|高于|大于|小于|超出|筛选|挑出|找出|不达标|未达标|超期|逾期|大额"
)


def _results_look_truncated(results: list, n_src: int, query: str,
                            metadata_rows=None, metadata_columns=None) -> int:
    """
    结果级偷懒验收(正则猜不全写法,行数骗不了人)。

    用户口径:**数据每一行都是独立记录,任何分析都不得合并行** ——
    "按大区 / 按产品线汇总"是排序方式,不是聚合压行;聚合 sheet 只能是
    逐行明细之外的附加 sheet。因此全量类问题必须有一张 sheet 行数 ≈ 数据行数
    (≥ 90%,容许剔除源合计行/坏行),否则返回最大 sheet 行数(疑似截断)。
    豁免:TOP-N、筛选类(异常/超期…)、小数据(<30 行)。
    """
    if n_src < 30:
        return 0
    q = query or ""
    if not _FULL_QUERY_PAT.search(q) or _TOPN_QUERY_PAT.search(q) or _FILTER_QUERY_PAT.search(q):
        return 0
    max_rows = 0
    for r in results or []:
        df = r.get("df")
        try:
            max_rows = max(max_rows, len(df))
        except Exception:
            continue
    need = max(30, int(n_src * 0.9))
    return max_rows if max_rows < need else 0


def _build_done_files(decision, results, cipher_path, excel_stem, skill_calls, clean_summary, log):
    """
    计算完成 → 按 decision 产出下载文件(status=done 结果片段):
      · decrypt        —— 同时产出 明文 Excel + 密文 Excel,前端两个并列下载
      · keep_encrypted —— 产出 密文 Excel + 把结果加密暂存沙盒(供前端「解密」按钮事后解出明文)
    任一写文件失败 → status=failed。
    """
    import secrets as _secrets
    try:
        # 密文文件加 _密文 后缀 —— 否则与明文同 stem+同秒时间戳会重名互相覆盖。
        # staging=True:写到沙盒暂存目录,不自动落 Downloads(用户点「下载」才存)。
        enc_path = export_skill_results_encrypted(results, cipher_path, stem=f"{excel_stem}_密文", staging=True)
    except Exception as e:
        return {"status": "failed", "error": f"密文 Excel 写入失败: {e}", "summary": clean_summary}

    if decision == "keep_encrypted":
        # 结果加密暂存 → 供「解密」按钮事后解出明文(明文不落盘,直到用户点解密)
        dec_run_id = _secrets.token_hex(8)
        try:
            from client.webui import sched_results
            sched_results.persist_results_encrypted(results, dec_run_id)
        except Exception:
            dec_run_id = ""   # 暂存失败 → 不提供事后解密,但密文文件仍可下载
        log("result", f"完成 · {enc_path.name}(保留密文)")
        return {
            "status": "done",
            "summary": clean_summary + " · 已生成密文版 Excel;如需明文结果,点文件旁「解密」即可。",
            "excel_path": "", "excel_name": "",
            "enc_excel_path": str(enc_path), "enc_excel_name": enc_path.name,
            "can_decrypt": bool(dec_run_id), "dec_run_id": dec_run_id, "dec_stem": excel_stem,
            "skill_calls": skill_calls, "error": "",
        }

    # decrypt:明文 + 密文 两个文件,用户自由选择下载哪个(均写暂存,不自动落 Downloads)
    try:
        n_src = max((len(r["df"]) for r in results if r.get("df") is not None), default=0)
        dec_path = write_skill_results(
            results, stem=excel_stem, staging=True,
            provenance={
                "数据文件": cipher_path.name,
                "执行方式": "固化技能" if skill_calls else "AI 生成代码(密态)",
                "结果最大行数": str(n_src),
            })
    except Exception as e:
        return {"status": "failed", "error": f"Excel 写入失败: {e}", "summary": clean_summary}
    log("result", f"完成 · 明文 {dec_path.name} + 密文 {enc_path.name}")
    return {
        "status": "done",
        "summary": clean_summary,
        "excel_path": str(dec_path), "excel_name": dec_path.name,
        "enc_excel_path": str(enc_path), "enc_excel_name": enc_path.name,
        "can_decrypt": False,
        "skill_calls": skill_calls, "error": "",
    }


_COMPOUND_MARKERS = ("、", "和", "并", "再", "同时", "然后", "以及", "+", "加上",
                     "分别", "各", "排名", "top", "标记", "预警", "异常",
                     "占比", "同比", "环比", "分类", "分群", "分箱")


def _looks_compound(q: str) -> bool:
    """粗判"复合分析问题"(多指标/多步骤),只对这类先做规划护栏,避免给简单问题平添一次 LLM 调用。"""
    q = (q or "").lower()
    if len(q) < 12:
        return False
    return sum(1 for m in _COMPOUND_MARKERS if m in q) >= 2


_MAX_CODEGEN_ERROR_RETRIES = 2   # 真实数据执行崩溃 → 带错误反馈重生成的最大次数


def _format_exec_error_feedback(exc: Exception, code: str) -> str:
    """
    把执行异常整理成给 LLM 的修正反馈。只回传**异常类型 + 异常消息 + 出错代码行**,
    异常消息里可能夹带的数据值风险极低(pandas 报错通常是列名/类型),但仍做一层零明文
    过滤,避免真实数据值随 KeyError/断言消息回流给 LLM(明文不出本机的底线)。
    """
    import traceback
    etype = type(exc).__name__
    emsg = str(exc)
    try:
        from client import permissions
        res = permissions.scan_summary(emsg)   # 检测异常消息里夹带的疑似明文数值
        if not res.clean:
            for h in sorted(res.hits, key=lambda x: x.start, reverse=True):
                emsg = emsg[:h.start] + "<已脱敏>" + emsg[h.end:]
    except Exception:  # noqa: BLE001
        pass
    # 定位出错代码行(用户生成代码在 <string> 里执行)
    lineno = None
    for fr in reversed(traceback.extract_tb(exc.__traceback__)):
        if fr.filename in ("<string>", "<generated>"):
            lineno = fr.lineno
            break
    hint = ""
    lines = code.splitlines()
    if lineno and 1 <= lineno <= len(lines):
        hint = f"\n出错代码行(第 {lineno} 行):{lines[lineno - 1].strip()[:120]}"
    common = {
        "KeyError": "列名拼错或该列不存在——列名严格照 schema,注意括号单位。",
        "TypeError": "很可能对 CipherSeries/CipherDataFrame 直接算数,或类型不匹配——先 df = ct.decrypt_df(cdf) 取明文再用 pandas 处理。",
        "AttributeError": "调了不存在的方法——检查是不是臆造了 API。",
        "ValueError": "数值/形状不匹配,或含 NaN/inf 未清洗——拟合/相关前先 s = s[np.isfinite(s)]。",
        "ZeroDivisionError": "除零——分母先 .replace(0, np.nan) 或判零。",
    }.get(etype, "")
    return (
        f"⚠️ 你上次的代码在**真实数据**上执行时抛了 {etype}:{emsg[:200]}。"
        f"{hint}\n可能原因:{common or '仔细检查上面这行。'}"
        "请修正后重写完整代码(整段自包含,别只贴补丁)。"
    )


def _run_codegen_path(
    *, effective_query, cipher_path, schema, metadata_rows, metadata_columns,
    host_url, token, history, custom_block, excel_stem,
    log, chk, prompt_decrypt, output_mode="interactive", run_id="",
    cache_key="", lazy_feedback="", error_feedback="", error_retries=0,
    web_search=False,
) -> Optional[dict]:
    """
    代码生成主路径:LLM 读 SKILL.md → 写代码 → AST 扫描 → 受限 exec → Excel。
    成功返回结果 dict;若任何环节失败,返回 None 让上层回退到固化 skill 路径。
    cancelled / keep_encrypted 是终态,直接返回(不回退)。

    output_mode:
      "interactive"        —— 正常:算完弹解密授权(HITL),解密/保留密文/取消
      "encrypted_sandbox"  —— 定时密态:自动允许计算,结果**加密暂存沙盒**,
                              不写明文 Excel,返回 encrypted_run(供批量解密)
    """
    # 解密授权后置(与固化路径同序):计算阶段在本机内存自动放行解密
    # (明文不出进程、不展示);算完 + 行数验收通过后,才向用户申请「解密展示」
    # 授权 —— 行数等验收都在授权前完成,偷懒重生成也不会反复弹授权。
    exec_prompt_decrypt = lambda: "decrypt"  # noqa: E731

    # 0) 定时任务代码固化缓存:命中则跳过 LLM,直接复用首次成功的代码
    cache_sig = ""
    from_cache = False
    code = ""
    summary_raw = ""
    lazy_waived = False
    unsafe_window = False   # 窗口函数未排序且修正失败 → 禁止固化缓存
    if cache_key:
        cache_sig = _codegen_cache_sig(effective_query, schema)
        cached = _codegen_cache_load(cache_key, cache_sig)
        if cached:
            code, summary_raw = cached["code"], cached["summary"]
            # 固化代码也过偷懒检测 —— 早期定板可能把 head()/切片截断固化了进去
            # (已豁免的不再重检,避免每次作废重生成、缓存名存实亡)
            if not cached["lazy_waived"] and _detect_lazy_truncation(effective_query, code):
                log("think", "固化代码截断了数据而用户要求全量 · 作废缓存,重新生成")
                _codegen_cache_delete(cache_key)
                code, summary_raw = "", ""
            else:
                from_cache = True
                log("think", "定时任务 · 复用首次固化的分析代码(每次运行结构保持一致)")

    if not from_cache:
        # 1) 意图路由选 SKILL.md
        skill_docs = skills_loader.route(effective_query)
        if not skill_docs:
            return None
        log("think", f"代码生成 · 加载技能文档:{' / '.join(d.name for d in skill_docs)}")

        # 2) LLM 写代码(定时任务首次生成是"定板":提示补全意图,产出将被固化复用)
        gen_query = effective_query
        if cache_key:
            gen_query = (
                f"{effective_query}\n\n"
                "(说明:这是**定时任务**的问题,会反复运行,本次生成的代码将被固化复用。"
                "即使问题很简短,也请一次性补全分析意图,按完整专业报表标准输出:"
                "关键指标排序+排名列、文字档位列、占比/合计、合适的图表;结构要经得起反复出表。)"
            )
        if lazy_feedback:
            gen_query = f"{gen_query}\n\n{lazy_feedback}"
        if error_feedback:
            gen_query = f"{gen_query}\n\n{error_feedback}"
        # 2.5) 复合问题:先出"步骤计划",用能力表校验(挡禁用算子/标授权解密),作为 codegen 脚手架。
        #      架构上仍由单代码块执行;计划只当护栏+提示,gated 到复合问题、全程围栏、失败即跳过。
        if _looks_compound(effective_query):
            try:
                from client.he_ops import planner as _planner
                _ps, _pu = _planner.build_plan_messages(effective_query, schema)
                _plan = _planner.parse_plan(
                    call_llm_for_codegen(host_url, token, _ps, _pu,
                                         history=history, should_cancel=chk, web_search=False))
                if _plan.steps:
                    _v = _planner.validate_plan(_plan)
                    log("think", f"已规划 {len(_plan.steps)} 步"
                        + (f" · 需授权解密步骤:{', '.join(_v.auth_steps)}" if _v.auth_steps else ""))
                    _fix = ("\n⚠ 修正:" + "; ".join(_v.errors)) if _v.errors else ""
                    gen_query = (gen_query +
                                 "\n\n参考下面已按算子能力校验过的步骤计划实现(用可靠算子,避开禁用的):\n"
                                 + _planner.plan_steps_text(_plan) + _fix)
            except CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 规划失败绝不拖垮主流程
                log("think", f"规划跳过({type(e).__name__})")
        system, user = codegen_mod.build_codegen_messages(
            skill_docs, schema, metadata_columns, gen_query, custom_block,
        )
        # 可信审计:记录"本次发给 LLM 的只有 schema 字段名 + 问题",附零明文断言
        try:
            from client.he_ops import audit as _audit
            _audit.record_llm_exposure(schema, gen_query, purpose="codegen")
        except Exception:  # noqa: BLE001 —— 审计失败不拖垮分析
            pass
        log("call", "调用 LLM 生成密态分析代码")
        if chk():
            raise CancelledError("用户已停止")
        raw = call_llm_for_codegen(host_url, token, system, user, history=history,
                                   should_cancel=chk, web_search=web_search)
        if chk():
            raise CancelledError("用户已停止")

        try:
            code, summary_raw = codegen_mod.extract_code(raw)
        except Exception as e:
            log("error", f"代码生成解析失败:{e} · 回退固化 skill")
            return None

        # 偷懒检测:用户要全量,代码却截断(head/sample/iloc[:N])→ 要求重写一次
        if _detect_lazy_truncation(effective_query, code):
            log("think", "检测到生成代码截断了数据,但用户要求全量 · 要求 LLM 重写")
            retry_user = (
                user + "\n\n⚠️ 你刚才的代码用 head()/sample()/iloc 截断了数据,"
                "但用户要求处理**全部数据行**。请重写:禁止任何截断,逐行全量处理。"
            )
            try:
                raw2 = call_llm_for_codegen(host_url, token, system, retry_user,
                                            history=history, should_cancel=chk,
                                            web_search=web_search)
                code2, summary2 = codegen_mod.extract_code(raw2)
                if not _detect_lazy_truncation(effective_query, code2):
                    code, summary_raw = code2, summary2
                    log("result", "重写完成 · 已改为全量处理")
                else:
                    lazy_waived = True  # 重写仍截断 · 存缓存时记豁免,下次不再反复作废
                    log("error", "重写后仍含截断 · 按原样继续(结果可能不全)")
            except Exception:
                lazy_waived = True
                log("error", "重写失败 · 按原代码继续(结果可能不全)")

        # 窗口函数未排序风险:环比/移动平均等按行序算,没排序会静默算错并被固化 → 修正一次
        if _detect_unsorted_window_risk(code):
            log("think", "检测到窗口函数(环比/移动平均等)但代码未排序 · 要求 LLM 加时间排序")
            retry_user = (
                user + "\n\n⚠️ 你的代码用了 diff/shift/pct_change/rolling/cumsum 等**按行序计算**的函数,"
                "但没有先按时间排序。这会算出**数值全错却不报错**的结果。请修正:调用这些函数前"
                "必须先 `df = df.sort_values('<时间列>')`;多实体数据要**逐实体** groupby 后各自算"
                "(如 `df.groupby('产品')['额'].diff()`),不能跨实体连着算。重写完整代码。"
            )
            try:
                raw_w = call_llm_for_codegen(host_url, token, system, retry_user,
                                             history=history, should_cancel=chk, web_search=web_search)
                code_w, summary_w = codegen_mod.extract_code(raw_w)
                if not _detect_unsorted_window_risk(code_w):
                    code, summary_raw = code_w, summary_w
                    log("result", "已加时间排序 · 采用修正后代码")
                else:
                    unsafe_window = True   # 仍未排序 · 禁止固化缓存(见下),避免每天复用错代码
                    log("error", "修正后仍未排序 · 本次不固化缓存,避免错误代码被反复复用")
            except CancelledError:
                raise
            except Exception:  # noqa: BLE001
                unsafe_window = True
                log("error", "窗口排序修正失败 · 本次不固化缓存")

    def _retry_without_cache(reason: str) -> Optional[dict]:
        """固化代码失效(数据形态变了等)→ 删缓存,本次就地重新生成一遍。"""
        log("error", f"{reason} · 固化代码失效,重新生成")
        _codegen_cache_delete(cache_key)
        return _run_codegen_path(
            effective_query=effective_query, cipher_path=cipher_path,
            schema=schema, metadata_rows=metadata_rows, metadata_columns=metadata_columns,
            host_url=host_url, token=token, history=history,
            custom_block=custom_block, excel_stem=excel_stem,
            log=log, chk=chk, prompt_decrypt=prompt_decrypt,
            output_mode=output_mode, run_id=run_id, cache_key=cache_key,
            web_search=web_search,
        )

    # 3) AST 安全扫描(缓存代码也重新扫,规则可能收紧过)
    try:
        codegen_mod.ast_safety_check(code)
    except codegen_mod.UnsafeCode as e:
        if from_cache:
            return _retry_without_cache(f"固化代码未通过安全扫描:{e}")
        log("error", f"生成代码未通过安全扫描:{e} · 回退固化 skill")
        return None
    log("result", "代码安全扫描通过")

    # 4) 加载 cipher
    log("call", f"加载密文 {cipher_path.name}")
    try:
        cdf = load_cipher_df(cipher_path)
    except Exception as e:
        log("error", f"密文加载失败:{e} · 回退固化 skill")
        return None

    # 4.5) 明文小样本校验(建议式):用按 schema 合成的几行随机数据先跑一遍同样的密态链路,
    #       几毫秒抓出列名错/崩溃/无产出等;硬失败→把错误反馈 LLM 重生成一次;**不硬拦**
    #       (合成数据本机随机造、自动解密体检,不出本机、不给 LLM;只对新生成代码,固化代码跳过)。
    if not from_cache:
        try:
            from client.he_ops.verifier import verify as _verify_code
            _ncols = [str(c) for c in getattr(cdf, "columns", [])]
            _vd = _verify_code(code, numeric_cols=_ncols, identity_cols=metadata_columns)
            if _vd.ok:
                log("result", f"小样本校验通过 · {_vd.summary()}")
            else:
                log("think", f"小样本校验未过:{_vd.error} · 反馈 LLM 重生成一次")
                _retry = (
                    user + f"\n\n⚠️ 你上次的代码在小样本上失败:{_vd.error}。常见原因:"
                    "列名拼错、忘了先 `df = ct.decrypt_df(cdf)`、对 CipherSeries 误用 ct.decrypt、未把结果放进 results。"
                    "请修正并重写完整代码。"
                )
                _raw3 = call_llm_for_codegen(host_url, token, system, _retry,
                                             history=history, should_cancel=chk, web_search=web_search)
                _code3, _summary3 = codegen_mod.extract_code(_raw3)
                codegen_mod.ast_safety_check(_code3)
                if _verify_code(_code3, numeric_cols=_ncols, identity_cols=metadata_columns).ok:
                    code, summary_raw = _code3, _summary3
                    log("result", "重生成后小样本校验通过 · 采用新代码")
                else:
                    log("error", "重生成仍未过 · 保留原代码,交由全量执行兜底")
        except CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 校验器自身异常绝不拖垮主流程
            log("think", f"小样本校验跳过(校验器异常:{type(e).__name__})")

    # 5) 受限执行(decrypt 首次触发解密授权)
    log("call", "受限执行生成代码 · 密态计算")
    try:
        results = codegen_mod.run_generated_code(
            code, cdf=cdf,
            metadata_rows=metadata_rows, metadata_columns=metadata_columns,
            prompt_decrypt=exec_prompt_decrypt, should_cancel=chk,
        )
    except codegen_mod.CodegenCancelled:
        log("error", "已停止 · 用户取消")
        return {"status": "cancelled", "summary": "", "error": "用户已停止", "excel_path": "", "skill_calls": []}
    except codegen_mod.KeepEncrypted:
        log("call", "用户选择保留密文 · 导出源密文 Excel")
        try:
            excel_path = export_cipher_as_is(cipher_path, metadata_rows, metadata_columns)
        except Exception as e:
            return {"status": "failed", "error": f"密文 Excel 写入失败: {e}", "summary": summary_raw}
        log("result", f"完成 · {excel_path.name}")
        return {
            "status": "done",
            "summary": "已按要求保留密文展示 · 数值列保持同态密文形式。若要明文结果请重新提问并选择「解密展示」。",
            "excel_path": str(excel_path), "skill_calls": ["codegen"], "error": "",
        }
    except codegen_mod.DecryptionFailed as e:
        # 解密失败是终态:密钥/密文不匹配、维度不符、密文损坏等。
        # 固化 skill 用同一套密钥/密文,回退只会再失败一次且掩盖真因 ——
        # 直接把原因报给用户,不回退。
        log("error", f"解密失败:{e} · 已停止(不回退固化 skill)")
        return {
            "status": "failed",
            "error": (
                f"解密失败:{e}。计算已在密态完成,但结果解密这一步出错。"
                "可重试一次;若反复出现,可能是密钥与该密文不是同一套 —— "
                "到「同态密钥」tab 重新拉取证书,或重新上传数据后再试。"
            ),
            "summary": "", "excel_path": "", "skill_calls": ["codegen"],
        }
    except Exception as e:
        if from_cache:
            return _retry_without_cache(f"固化代码执行失败:{e}")
        # 真实数据上执行崩溃 —— README 承诺的"回环自修复":把具体报错回喂 LLM 重生成,
        # 而不是静默切到另一套固化 skill(那会给出结构迥异的结果或再撞同一个坑)。
        if error_retries < _MAX_CODEGEN_ERROR_RETRIES:
            fb = _format_exec_error_feedback(e, code)
            log("error", f"代码执行失败:{type(e).__name__}: {e} · "
                         f"带报错反馈重生成(第 {error_retries + 1}/{_MAX_CODEGEN_ERROR_RETRIES} 次)")
            return _run_codegen_path(
                effective_query=effective_query, cipher_path=cipher_path,
                schema=schema, metadata_rows=metadata_rows, metadata_columns=metadata_columns,
                host_url=host_url, token=token, history=history,
                custom_block=custom_block, excel_stem=excel_stem,
                log=log, chk=chk, prompt_decrypt=prompt_decrypt,
                output_mode=output_mode, run_id=run_id, cache_key=cache_key,
                web_search=web_search, lazy_feedback=lazy_feedback,
                error_feedback=fb, error_retries=error_retries + 1,
            )
        log("error", f"代码执行失败且重生成 {error_retries} 次仍未通过:{e} · 回退固化 skill")
        return None

    if not results:
        if from_cache:
            return _retry_without_cache("固化代码没产出结果")
        if error_retries < _MAX_CODEGEN_ERROR_RETRIES:
            log("error", "生成代码没产出结果 · 带反馈重生成")
            return _run_codegen_path(
                effective_query=effective_query, cipher_path=cipher_path,
                schema=schema, metadata_rows=metadata_rows, metadata_columns=metadata_columns,
                host_url=host_url, token=token, history=history,
                custom_block=custom_block, excel_stem=excel_stem,
                log=log, chk=chk, prompt_decrypt=prompt_decrypt,
                output_mode=output_mode, run_id=run_id, cache_key=cache_key,
                web_search=web_search, lazy_feedback=lazy_feedback,
                error_feedback=(
                    "⚠️ 你上次的代码跑完没有把任何结果放进 results 列表。"
                    "请确保 results = [{'sheet_name':..., 'df':...}] 至少有一个元素。"
                ),
                error_retries=error_retries + 1,
            )
        log("error", "生成代码反复无产出 · 回退固化 skill")
        return None
    log("result", f"密态计算完成 · {len(results)} 个 sheet")

    # 结果级偷懒验收:用户要全量但所有 sheet 行数都远小于数据行数 → 行数骗不了人
    # (按身份实体聚合的行数 == 实体去重数,属合法全量,已在检查内豁免)
    n_src = len(metadata_rows or [])
    trunc_rows = _results_look_truncated(results, n_src, effective_query,
                                         metadata_rows, metadata_columns)
    if trunc_rows:
        if from_cache:
            return _retry_without_cache(
                f"固化代码输出行数过少({trunc_rows} 行,数据 {n_src} 行)疑似截断")
        if not lazy_feedback:
            log("error",
                f"结果疑似截断:数据 {n_src} 行,最大 sheet 仅 {trunc_rows} 行 · 带反馈重新生成")
            return _run_codegen_path(
                effective_query=effective_query, cipher_path=cipher_path,
                schema=schema, metadata_rows=metadata_rows, metadata_columns=metadata_columns,
                host_url=host_url, token=token, history=history,
                custom_block=custom_block, excel_stem=excel_stem,
                log=log, chk=chk, prompt_decrypt=prompt_decrypt,
                output_mode=output_mode, run_id=run_id, cache_key=cache_key,
                web_search=web_search,
                lazy_feedback=(
                    f"⚠️ 数据共 {n_src} 行,**每一行都是独立记录,不得合并行**;"
                    f"但你上次只输出了 {trunc_rows} 行。请改为**逐行明细**:行数 = 数据行数。"
                    "「按大区 / 按产品线 / 汇总」指的是**排序方式**(先按该维度排、"
                    "组内再按指标排),不是 groupby 压行;"
                    "聚合视角只能作为逐行明细之外的**附加** sheet。"
                ),
            )
        lazy_waived = True  # 重试过仍偏少 · 按现有结果继续,缓存记豁免
        log("think", f"重新生成后最大 sheet 仍只有 {trunc_rows} 行 · 按现有结果继续")

    # 首次成功 → 固化本任务的分析代码,之后每次到点复用(输出结构一致)
    # 窗口函数未排序且修正失败的代码绝不固化——否则错误结果会被定时任务每天复用。
    if cache_key and not from_cache and not unsafe_window:
        _codegen_cache_save(cache_key, cache_sig, code, summary_raw, lazy_waived=lazy_waived)
        log("think", "已固化本任务的分析代码 · 后续每次运行保持同一结构")
    elif cache_key and unsafe_window:
        log("think", "本次代码有未排序窗口风险 · 不固化,下次运行会重新生成")

    # 6a) 定时密态模式:结果加密暂存沙盒,不写明文 Excel
    if output_mode == "encrypted_sandbox":
        from client.webui import sched_results
        log("call", "结果加密暂存(不解密)· 待批量解密")
        try:
            manifest = sched_results.persist_results_encrypted(results, run_id)
        except Exception as e:
            return {"status": "failed", "error": f"结果加密暂存失败: {e}", "summary": summary_raw}
        log("result", f"已加密暂存 {len(manifest)} 张表 · 待你批量解密")
        return {
            "status": "encrypted_pending",
            "summary": "密态计算已完成 · 结果已加密暂存(未解密)· 在「定时任务 → 待批运行」批量解密。",
            "excel_path": "", "skill_calls": ["codegen"], "error": "",
            "encrypted_run": {"run_id": run_id, "manifest": manifest},
        }

    # 6b) 解密展示授权(后置 · 与固化路径同序):计算与验收都已完成,才问用户
    decision = "decrypt"
    if prompt_decrypt:
        log("think", f"密态计算完成 · 等待解密展示授权({len(results)} 个 sheet)")
        try:
            decision = prompt_decrypt() or "decrypt"
        except CancelledError:
            log("error", "已停止 · 用户取消")
            return {"status": "cancelled", "summary": summary_raw, "error": "用户已停止",
                    "excel_path": "", "skill_calls": ["codegen"]}
        log("result", f"用户选择:{'解密展示' if decision == 'decrypt' else '保留密文展示' if decision == 'keep_encrypted' else '取消'}")
    # 可信审计:记录解密授权台账(谁/何时/哪个会话/授权解密还是保留密文)
    try:
        from client.he_ops import audit as _audit
        _dec = "granted" if decision == "decrypt" else "keep_encrypted" if decision == "keep_encrypted" else "denied"
        _audit.record_decrypt_auth(_dec, detail=f"{len(results)} 个 sheet 解密展示")
    except Exception:  # noqa: BLE001
        pass
    if decision == "cancel":
        return {"status": "cancelled", "summary": summary_raw, "error": "用户已停止",
                "excel_path": "", "skill_calls": ["codegen"]}

    # 6c) 产出下载文件(decrypt=明文+密文两个;keep_encrypted=密文+可事后解密)
    fr = scan_summary(summary_raw)
    clean = summary_raw if fr.clean else "已生成分析,详见 Excel(summary 命中明文规则已隐去)。"
    log("call", "产出 Excel" + ("(明文+密文)" if decision == "decrypt" else "(密文)"))
    return _build_done_files(decision, results, cipher_path, excel_stem, ["codegen"], clean, log)


def ask(**kwargs) -> dict:
    """外层入口:重置本次 token 累计,跑完后把总用量注入 result["tokens"]。"""
    _usage_reset()
    result = _ask_impl(**kwargs)
    if isinstance(result, dict):
        result.setdefault("tokens", _usage_total())
    return result


def _ask_impl(
    *,
    user_query: str,
    cipher_path: Optional[Path],
    host_url: str,
    token: str,
    system_prompt: str,
    on_step: Optional[StepCallback] = None,
    should_cancel: Optional[ShouldCancel] = None,
    history: Optional[list[dict]] = None,
    text_attachments: Optional[list[dict]] = None,
    prompt_decrypt: Optional[Callable[[], str]] = None,
    custom_block: str = "",
    output_mode: str = "interactive",
    run_id: str = "",
    codegen_cache_key: str = "",
    web_search: bool = False,
    audit_user: str = "",
    audit_session: str = "",
) -> dict:
    """
    跑一次完整分析。返回:
      {
        status: "done" | "failed" | "cancelled",
        summary: str,
        excel_path: str,
        skill_calls: list[str],
        error: str,
      }
    """
    log = on_step or (lambda kind, label: None)
    chk = should_cancel or (lambda: False)

    # 可信审计:设请求作用域上下文(user, session),供各埋点记录(不层层穿参)
    try:
        from client.he_ops import audit as _audit
        _audit.set_context(audit_user, audit_session or run_id)
    except Exception:  # noqa: BLE001
        pass

    def _ck():
        if chk():
            raise CancelledError("用户已停止")

    # 把文本附件折到 user_query 顶部 —— 后续所有 LLM 调用都用这个版本
    effective_query = _fold_text_attachments(user_query, text_attachments)
    if text_attachments:
        names = [a.get("name", "") for a in text_attachments if a.get("content")]
        if names:
            log("think", f"读取文本附件 · {' · '.join(names)}")

    # 0) 意图识别 —— 不像分析就走自由聊天(允许"没附密文也能聊天")
    # 用原始 user_query 判断,不让附件内容干扰意图判断
    is_analysis = looks_like_analysis(user_query)
    # 联网与否完全由用户的「联网搜索」开关决定:开=可联网,关=不联网(不擅自跳过按钮)。
    # 但若问的是实时信息却没开联网 → 在回复前加一句提示,告诉用户开开关,而不是让模型干巴巴拒绝。
    need_web_tip = (not web_search) and _looks_like_web_lookup(user_query)
    _WEB_TIP = ("> 💡 你问的是**实时信息**,但「联网搜索」未开启,以下仅基于模型已有知识。\n"
                "> 需要实时结果?点输入框左侧的 🌐 **联网搜索** 按钮打开后再问一次。\n\n")

    def _freechat_result(text: str) -> dict:
        if need_web_tip:
            text = _WEB_TIP + text
        return {"status": "done", "summary": text, "excel_path": "", "skill_calls": [], "error": ""}

    try:
        # 1) 没附密文 → 自由聊天(LLM 直接回答)
        if cipher_path is None:
            log("think", "未附密文文件 · 自由聊天模式")
            log("call", "调用 LLM(freechat)" + (" · 联网搜索" if web_search else ""))
            _ck()
            text = call_llm_for_freechat(host_url, token, effective_query, history=history, should_cancel=chk, web_search=web_search)
            _ck()
            log("result", "已回复")
            return _freechat_result(text)

        # 2) 有密文但意图不像分析 → 仍走自由聊天
        if not is_analysis:
            log("think", f"已附密文「{cipher_path.name}」· 但问题不像数据分析 · 自由聊天模式")
            log("call", "调用 LLM(freechat)" + (" · 联网搜索" if web_search else ""))
            _ck()
            text = call_llm_for_freechat(host_url, token, effective_query, history=history, should_cancel=chk, web_search=web_search)
            _ck()
            log("result", "已回复")
            return _freechat_result(text)
    except CancelledError:
        log("error", "已停止 · 用户取消")
        return {"status": "cancelled", "summary": "", "error": "用户已停止", "excel_path": "", "skill_calls": []}
    except PermissionError:
        return {"status": "failed", "error": "登录已过期 · 请重新登录", "summary": ""}
    except Exception as e:
        return {"status": "failed", "error": f"LLM 调用失败: {e}", "summary": ""}

    if not cipher_path.exists():
        return {"status": "failed", "error": f"密文文件不存在: {cipher_path}", "summary": ""}

    log("think", f"识别意图:数据分析 · 文件「{cipher_path.name}」")

    # 2) 加载 sidecar
    schema = load_schema(cipher_path)
    if not schema:
        return {
            "status": "failed",
            "error": "密文文件缺失 schema sidecar · 请删除并重传以触发自动识别",
            "summary": "",
        }
    metadata_rows, metadata_columns = load_metadata(cipher_path)
    if metadata_rows:
        log("think", f"加载身份列 sidecar · {len(metadata_rows)} 行 · 列: {', '.join(metadata_columns[:6])}")

    excel_stem = derive_excel_stem(cipher_path, user_query)

    # ───────────────────────────────────────────────────────────
    # 主路径:代码生成(LLM 读 SKILL.md 写代码 → 安全执行)
    # 任一环节失败返回 None → 自动回退到下面的固化 skill 路径
    # ───────────────────────────────────────────────────────────
    try:
        cg = _run_codegen_path(
            effective_query=effective_query, cipher_path=cipher_path,
            schema=schema, metadata_rows=metadata_rows, metadata_columns=metadata_columns,
            host_url=host_url, token=token, history=history,
            custom_block=custom_block, excel_stem=excel_stem,
            log=log, chk=chk, prompt_decrypt=prompt_decrypt,
            output_mode=output_mode, run_id=run_id,
            cache_key=codegen_cache_key, web_search=web_search,
        )
    except CancelledError:
        log("error", "已停止 · 用户取消")
        return {"status": "cancelled", "summary": "", "error": "用户已停止", "excel_path": "", "skill_calls": []}
    except PermissionError:
        return {"status": "failed", "error": "登录已过期 · 请重新登录", "summary": ""}
    except codegen_mod.DecryptionFailed as e:
        # 防御:解密失败是终态,绝不回退固化 skill
        log("error", f"解密失败:{e} · 已停止(不回退固化 skill)")
        return {
            "status": "failed",
            "error": (
                f"解密失败:{e}。通常是密钥/密文不匹配或密文损坏 —— "
                "请确认本机密钥与该密文是同一套后重试。"
            ),
            "summary": "", "excel_path": "", "skill_calls": ["codegen"],
        }
    except Exception as e:
        log("error", f"代码生成路径异常:{e} · 回退固化 skill")
        cg = None
    if cg is not None:
        return cg

    log("think", "回退固化 skill 路径")

    # 3) 调 LLM 拿 plan
    log("call", "调用 LLM 生成 skill_calls 计划")
    try:
        _ck()
        plan, summary_raw = call_llm_for_plan(
            host_url, token, system_prompt, effective_query, schema,
            history=history, should_cancel=chk,
        )
        _ck()
    except CancelledError:
        log("error", "已停止 · 用户取消")
        return {"status": "cancelled", "summary": "", "error": "用户已停止", "excel_path": "", "skill_calls": []}
    except PermissionError as e:
        return {"status": "failed", "error": "登录已过期 · 请重新登录", "summary": ""}
    except Exception as e:
        return {"status": "failed", "error": f"LLM 调用失败: {e}", "summary": ""}

    if not plan.skill_calls:
        return {
            "status": "failed",
            "error": "LLM 没给出任何 skill_call · 重试或换个模型",
            "summary": summary_raw,
        }

    log("result", f"plan 解析 OK · {len(plan.skill_calls)} 个 skill 待执行")

    # 校验 + 自动修复 LLM 输出偏差(漏写 compute、漏填 num_col 等)
    plan, plan_warnings = validate_and_repair_plan(plan, schema, log_fn=log)
    if plan_warnings:
        fix_count = sum(1 for w in plan_warnings if w.startswith("fixed:"))
        warn_count = sum(1 for w in plan_warnings if w.startswith("warn:"))
        if fix_count:
            log("think", f"plan 校验 · 自动修复 {fix_count} 处指标缺失")
        if warn_count:
            log("think", f"plan 校验 · {warn_count} 处未硬编码 · 进入 LLM 回环修正")
            try:
                _ck()
                plan, summary_repaired = call_llm_for_plan_repair(
                    host_url, token, system_prompt, user_query, schema,
                    plan, plan_warnings,
                    history=history, should_cancel=chk,
                )
                _ck()
                summary_raw = summary_repaired or summary_raw
                log("result", f"LLM 回环修正成功 · {len(plan.skill_calls)} 个 skill")
                # 二次校验 —— 这次只补不再回环,避免死循环
                plan, plan_warnings2 = validate_and_repair_plan(plan, schema, log_fn=log)
                fix2 = sum(1 for w in plan_warnings2 if w.startswith("fixed:"))
                warn2 = sum(1 for w in plan_warnings2 if w.startswith("warn:"))
                if fix2:
                    log("think", f"二次校验 · 又自动补了 {fix2} 处")
                if warn2:
                    log("think", f"二次校验仍有 {warn2} 处未修复 · 按现有 plan 继续(用户可在 Excel 自检)")
            except CancelledError:
                raise
            except PermissionError:
                return {"status": "failed", "error": "登录已过期 · 请重新登录", "summary": summary_raw}
            except Exception as e:
                log("error", f"LLM 回环修正失败:{e} · 用原 plan 继续")

    # 4) 加载 cipher(读密文 · CipherDataFrame · 不涉及解密)
    log("call", f"加载密文 {cipher_path.name}")
    try:
        _ck()
        cdf = load_cipher_df(cipher_path)
    except CancelledError:
        log("error", "已停止 · 用户取消")
        return {"status": "cancelled", "summary": "", "error": "用户已停止", "excel_path": "", "skill_calls": []}
    except Exception as e:
        return {"status": "failed", "error": f"密文加载失败: {e}", "summary": summary_raw}

    # 5) 密态计算 —— 每个 SkillCall 在密文上跑(各 skill 计算路径不同)
    log("think", "进入密态计算阶段 · 计算全程不暴露明文")
    results: list[dict] = []
    for i, sc in enumerate(plan.skill_calls, 1):
        try:
            _ck()
        except CancelledError:
            log("error", "已停止 · 用户取消")
            return {"status": "cancelled", "summary": summary_raw, "error": "用户已停止", "excel_path": "", "skill_calls": [sc.skill for sc in plan.skill_calls[:i-1]]}
        skill_def = SKILLS.get(sc.skill)
        if not skill_def:
            return {
                "status": "failed",
                "error": f"未知 skill「{sc.skill}」(第 {i} 个)· 可用: {list(SKILLS.keys())}",
                "summary": summary_raw,
            }
        desc = skill_def.get("desc", sc.skill)
        log("call", f"({i}/{len(plan.skill_calls)}) 密态运算 · {sc.skill} · {desc[:30]}")
        try:
            sheet_name, df, chart_hint = run_skill(
                sc.skill, cdf, sc.params or {},
                metadata_rows, metadata_columns,
            )
            if sc.sheet_name:
                sheet_name = sc.sheet_name
            chart = sc.chart.model_dump() if sc.chart else chart_hint
            results.append({"sheet_name": sheet_name, "df": df, "chart": chart, "skill": sc.skill,
                            "note": skill_def.get("note", "")})   # 口径说明渲染在表顶
            log("result", f"sheet「{sheet_name}」就绪 · {len(df)} 行 × {len(df.columns)} 列")
        except Exception as e:
            return {
                "status": "failed",
                "error": f"skill「{sc.skill}」执行失败: {e}",
                "summary": summary_raw,
            }

    # 定时密态模式(固化兜底也支持):结果加密暂存,不弹解密授权
    if output_mode == "encrypted_sandbox":
        from client.webui import sched_results
        log("call", "结果加密暂存(不解密)· 待批量解密")
        try:
            manifest = sched_results.persist_results_encrypted(results, run_id)
        except Exception as e:
            return {"status": "failed", "error": f"结果加密暂存失败: {e}", "summary": summary_raw}
        log("result", f"已加密暂存 {len(manifest)} 张表 · 待你批量解密")
        return {
            "status": "encrypted_pending",
            "summary": "密态计算已完成 · 结果已加密暂存(未解密)· 在「定时任务 → 待批运行」批量解密。",
            "excel_path": "", "skill_calls": [r["skill"] for r in results], "error": "",
            "encrypted_run": {"run_id": run_id, "manifest": manifest},
        }

    # ──────────────────────────────────────────────
    # 解密授权(Human-in-the-Loop):计算已在密态完成,问用户结果是否解密展示
    # 选项:decrypt(解密后写 Excel)/ keep_encrypted(导出密文文件)/ cancel
    # ──────────────────────────────────────────────
    decision = "decrypt"
    if prompt_decrypt:
        log("think", f"密态计算完成 · 等待解密展示授权({len(results)} 个 sheet)")
        try:
            decision = prompt_decrypt() or "decrypt"
        except CancelledError:
            log("error", "已停止 · 用户取消")
            return {"status": "cancelled", "summary": summary_raw, "error": "用户已停止", "excel_path": "", "skill_calls": [r["skill"] for r in results]}
        log("result", f"用户选择:{'解密展示' if decision == 'decrypt' else '保留密文展示' if decision == 'keep_encrypted' else '取消'}")

    if decision == "cancel":
        return {"status": "cancelled", "summary": summary_raw, "error": "用户已停止", "excel_path": "", "skill_calls": [r["skill"] for r in results]}

    # 6) 产出下载文件(decrypt=明文+密文两个;keep_encrypted=密文+可事后解密)· summary 零明文过滤
    fr = scan_summary(summary_raw)
    clean = summary_raw if fr.clean else "已生成多 sheet 分析,详见 Excel(模型 summary 命中明文规则,已隐去)。"
    log("call", "产出 Excel" + ("(明文+密文)" if decision == "decrypt" else "(密文)"))
    return _build_done_files(decision, results, cipher_path, excel_stem,
                             [r["skill"] for r in results], clean, log)
