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


def looks_like_analysis(user_query: str) -> bool:
    """启发式:是否像数据分析意图。否 → 走自由聊天端点。"""
    if not user_query:
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
    "用户现在没有附加密文文件 / 也没提数据分析诉求,你只是和他闲聊。"
    "请用简洁、礼貌的中文回答。不要输出 <computation_plan> 或 JSON 块。"
    "如果用户问起怎么用,可以提醒:点击下方回形针按钮选一份已加密文件,"
    "再问类似「按大区统计完成率」「TOP10 销售」这样的问题即可生成 Excel 报表。"
)


def call_llm_for_freechat(
    host_url: str, token: str, user_query: str,
    history: Optional[list[dict]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    timeout: float = 1800.0,
) -> str:
    """调 host /llm/freechat,返回纯文本回复。history 可选透传。"""
    r = _post_cancellable(
        f"{host_url}/llm/freechat",
        headers={"Authorization": f"Bearer {token}"},
        json_body={
            "system": _FREECHAT_SYSTEM,
            "user": user_query,
            "history": history or [],
        },
        timeout=timeout,
        should_cancel=should_cancel,
    )
    if r.status_code == 401:
        raise PermissionError("登录已过期")
    r.raise_for_status()
    body = r.json()
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
    if "computation_plan" in body and "summary" in body:
        plan = ComputationPlan.model_validate(body["computation_plan"])
        return plan, body["summary"]
    raise ValueError(f"repair LLM 返回未知格式: {list(body.keys())[:5]}")


def call_llm_for_codegen(
    host_url: str, token: str, system: str, user: str,
    history: Optional[list[dict]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    timeout: float = 1800.0,
) -> str:
    """调 host /llm/freechat 拿原始文本(含 ```python``` 代码块 + summary)。"""
    r = _post_cancellable(
        f"{host_url}/llm/freechat",
        headers={"Authorization": f"Bearer {token}"},
        json_body={"system": system, "user": user, "history": history or []},
        timeout=timeout,
        should_cancel=should_cancel,
    )
    if r.status_code == 401:
        raise PermissionError("登录已过期")
    r.raise_for_status()
    body = r.json()
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


def _run_codegen_path(
    *, effective_query, cipher_path, schema, metadata_rows, metadata_columns,
    host_url, token, history, custom_block, excel_stem,
    log, chk, prompt_decrypt, output_mode="interactive", run_id="",
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
    # 定时密态模式:计算阶段自动放行解密(在本机内存算),结果保持密文
    exec_prompt_decrypt = prompt_decrypt
    if output_mode == "encrypted_sandbox":
        exec_prompt_decrypt = lambda: "decrypt"  # noqa: E731
    # 1) 意图路由选 SKILL.md
    skill_docs = skills_loader.route(effective_query)
    if not skill_docs:
        return None
    log("think", f"代码生成 · 加载技能文档:{' / '.join(d.name for d in skill_docs)}")

    # 2) LLM 写代码
    system, user = codegen_mod.build_codegen_messages(
        skill_docs, schema, metadata_columns, effective_query, custom_block,
    )
    log("call", "调用 LLM 生成密态分析代码")
    if chk():
        raise CancelledError("用户已停止")
    raw = call_llm_for_codegen(host_url, token, system, user, history=history, should_cancel=chk)
    if chk():
        raise CancelledError("用户已停止")

    try:
        code, summary_raw = codegen_mod.extract_code(raw)
    except Exception as e:
        log("error", f"代码生成解析失败:{e} · 回退固化 skill")
        return None

    # 3) AST 安全扫描
    try:
        codegen_mod.ast_safety_check(code)
    except codegen_mod.UnsafeCode as e:
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
        log("error", f"代码执行失败:{e} · 回退固化 skill")
        return None

    if not results:
        log("error", "生成代码没产出结果 · 回退固化 skill")
        return None
    log("result", f"密态计算完成 · {len(results)} 个 sheet")

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

    # 6b) 写明文 Excel
    log("call", "写入 Excel")
    try:
        excel_path = write_skill_results(results, stem=excel_stem)
    except Exception as e:
        return {"status": "failed", "error": f"Excel 写入失败: {e}", "summary": summary_raw}
    log("result", f"完成 · {excel_path.name}")

    fr = scan_summary(summary_raw)
    summary_clean = summary_raw if fr.clean else "已生成分析,详见 Excel(summary 命中明文规则已隐去)。"
    return {
        "status": "done", "summary": summary_clean,
        "excel_path": str(excel_path),
        "skill_calls": ["codegen"], "error": "",
    }


def ask(
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

    try:
        # 1) 没附密文 → 自由聊天(LLM 直接回答)
        if cipher_path is None:
            log("think", "未附密文文件 · 自由聊天模式")
            log("call", "调用 LLM(freechat)")
            _ck()
            text = call_llm_for_freechat(host_url, token, effective_query, history=history, should_cancel=chk)
            _ck()
            log("result", "已回复")
            return {
                "status": "done", "summary": text,
                "excel_path": "", "skill_calls": [], "error": "",
            }

        # 2) 有密文但意图不像分析 → 仍走自由聊天
        if not is_analysis:
            log("think", f"已附密文「{cipher_path.name}」· 但问题不像数据分析 · 自由聊天模式")
            log("call", "调用 LLM(freechat)")
            _ck()
            text = call_llm_for_freechat(host_url, token, effective_query, history=history, should_cancel=chk)
            _ck()
            log("result", "已回复")
            return {
                "status": "done", "summary": text,
                "excel_path": "", "skill_calls": [], "error": "",
            }
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
            results.append({"sheet_name": sheet_name, "df": df, "chart": chart, "skill": sc.skill})
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

    if decision == "keep_encrypted":
        log("call", f"重新加密 {len(results)} 个 sheet · 数值列保持密文")
        try:
            excel_path = export_skill_results_encrypted(
                results, cipher_path, stem=excel_stem,
            )
        except Exception as e:
            return {"status": "failed", "error": f"密文 Excel 写入失败: {e}", "summary": summary_raw}
        log("result", f"完成 · {excel_path.name}")
        cipher_summary = (
            f"密态计算已完成 · 已按要求**保留密文展示** · 共 {len(results)} 个 sheet。"
            f"输出 Excel 与解密版结构一致(身份列 + 数值列),但数值列保持同态密文 (base64) 形式;"
            f"「说明」sheet 列出了 skill → sheet 映射。若要查看明文结果请重新提问并选择「解密展示」。"
        )
        return {
            "status": "done",
            "summary": cipher_summary,
            "excel_path": str(excel_path),
            "skill_calls": [r["skill"] for r in results],
            "error": "",
        }

    # 6) 写 Excel(decision == "decrypt" · 解密后正常输出)
    log("call", "写入 Excel")
    try:
        excel_path = write_skill_results(results, stem=excel_stem)
    except Exception as e:
        return {"status": "failed", "error": f"Excel 写入失败: {e}", "summary": summary_raw}
    log("result", f"完成 · {excel_path.name}")

    # 7) summary B6-3 零明文过滤
    fr = scan_summary(summary_raw)
    summary_clean = summary_raw if fr.clean else "已生成多 sheet 分析,详见 Excel(模型 summary 命中明文规则,已隐去)。"

    return {
        "status": "done",
        "summary": summary_clean,
        "excel_path": str(excel_path),
        "skill_calls": [sc.skill for sc in plan.skill_calls],
        "error": "",
    }
