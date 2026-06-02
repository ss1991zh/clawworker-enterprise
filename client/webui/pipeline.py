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
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from client.tools.runtime import Runtime
from client.tools.skills import run_skill, SKILLS
from client.webui.writer import write_skill_results
from client.permissions import scan_summary
from shared.contract import ComputationPlan


StepCallback = Callable[[str, str], None]  # (kind, label)
# kind: think | call | result | error
# label: 一行简短描述,前端直接显示


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


def call_llm_for_plan(
    host_url: str, token: str, system_prompt: str, user_query: str, schema: dict,
    timeout: float = 180.0,
) -> tuple[ComputationPlan, str]:
    """调 host /llm/chat,返回 (plan, summary)。"""
    user_msg = (
        f"用户问题:\n{user_query}\n\n"
        f"数据 schema(只有字段名,没有明文数据):\n"
        f"{json.dumps(schema, ensure_ascii=False, indent=2)}\n\n"
        f"请按 system prompt 输出 computation_plan + summary。"
    )
    r = httpx.post(
        f"{host_url}/llm/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"system": system_prompt, "user": user_msg},
        timeout=httpx.Timeout(timeout, connect=5.0),
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


def ask(
    *,
    user_query: str,
    cipher_path: Optional[Path],
    host_url: str,
    token: str,
    system_prompt: str,
    on_step: Optional[StepCallback] = None,
) -> dict:
    """
    跑一次完整分析。返回:
      {
        status: "done" | "failed" | "needs_cipher",
        summary: str,
        excel_path: str,
        skill_calls: list[str],   # 跑了哪些 skill
        error: str,
      }
    """
    log = on_step or (lambda kind, label: None)

    # 1) 必须有密文
    if cipher_path is None:
        log("think", "未附密文文件 · 等待用户上传或指定")
        return {
            "status": "needs_cipher",
            "summary": "请上传一份加密数据文件,或在消息里指定要分析哪份已加密的文件。",
            "error": "",
        }
    if not cipher_path.exists():
        return {"status": "failed", "error": f"密文文件不存在: {cipher_path}", "summary": ""}

    log("think", f"识别意图 · 数据文件「{cipher_path.name}」")

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

    # 3) 调 LLM 拿 plan
    log("call", "调用 LLM 生成 skill_calls 计划")
    try:
        plan, summary_raw = call_llm_for_plan(host_url, token, system_prompt, user_query, schema)
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

    # 4) 加载 cipher
    log("call", f"加载密文 {cipher_path.name}")
    try:
        cdf = load_cipher_df(cipher_path)
    except Exception as e:
        return {"status": "failed", "error": f"密文加载失败: {e}", "summary": summary_raw}

    # 5) 跑每个 SkillCall
    results: list[dict] = []
    for i, sc in enumerate(plan.skill_calls, 1):
        skill_def = SKILLS.get(sc.skill)
        if not skill_def:
            return {
                "status": "failed",
                "error": f"未知 skill「{sc.skill}」(第 {i} 个)· 可用: {list(SKILLS.keys())}",
                "summary": summary_raw,
            }
        desc = skill_def.get("desc", sc.skill)
        log("call", f"({i}/{len(plan.skill_calls)}) {sc.skill} · {desc[:30]}")
        try:
            sheet_name, df, chart_hint = run_skill(
                sc.skill, cdf, sc.params or {},
                metadata_rows, metadata_columns,
            )
            if sc.sheet_name:
                sheet_name = sc.sheet_name
            chart = sc.chart.model_dump() if sc.chart else chart_hint
            results.append({"sheet_name": sheet_name, "df": df, "chart": chart})
            log("result", f"sheet「{sheet_name}」就绪 · {len(df)} 行 × {len(df.columns)} 列")
        except Exception as e:
            return {
                "status": "failed",
                "error": f"skill「{sc.skill}」执行失败: {e}",
                "summary": summary_raw,
            }

    # 6) 写 Excel
    log("call", "写入 Excel")
    try:
        excel_path = write_skill_results(results)
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
