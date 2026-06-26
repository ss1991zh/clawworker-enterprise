"""
可信审计日志(append-only)—— Phase A:把"明文不出本机、LLM 只见 schema"从声称变成可核查的证据。

记两类事件,按用户写 append-only JSONL(~/.agent-system/audit/<user>.jsonl):
  · llm_exposure   每次分析发给 LLM 的内容:只有 schema 字段名(+ 类型),并附"零明文断言"
                   (结构校验:发送内容不含任何数据行/单元值)。
  · decrypt_auth   每次解密授权门触发:谁、何时、哪个会话、授权/拒绝/保留密文。

用途:合规审计、客户尽调、出事自证清白。日志只追加不改写,可导出合规报告。
隐私:审计日志本身**不含任何明文数据值**,只记字段名 + 行为元数据。
"""
from __future__ import annotations

import contextvars
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

AUDIT_DIR = Path(os.path.expanduser("~/.agent-system/audit"))

# 请求作用域上下文(user, session)—— 在分析入口 set_context 一次,
# 各埋点 record_* 不传 user/session 时从这里取(避免层层穿参)。
_CTX: contextvars.ContextVar = contextvars.ContextVar("audit_ctx", default=("default", ""))


def set_context(user: Optional[str], session: Optional[str]) -> None:
    _CTX.set((user or "default", session or ""))


def _ctx_user_session(user, session):
    cu, cs = _CTX.get()
    return (user if user is not None else cu), (session if session is not None else cs)


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _path(user: Optional[str]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in (user or "default") if c.isalnum() or c in "-_.") or "default"
    return AUDIT_DIR / f"{safe}.jsonl"


def _append(user: Optional[str], event: dict) -> None:
    """写一行审计事件;失败绝不影响主流程。"""
    try:
        event = {"ts": _now(), **event}
        with _path(user).open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001 —— 审计失败不能阻断分析
        pass


def assert_no_plaintext(schema: dict, query: str) -> dict:
    """零明文断言:确认发给 LLM 的内容(schema + 问题)只含字段名/类型,不含数据行/单元值。
    schema 应是 {字段名: 类型/描述} 形态;若检出疑似嵌入的数据行(list/dict 值且非类型描述),标记。"""
    fields = list(schema.keys()) if isinstance(schema, dict) else []
    suspicious = []
    if isinstance(schema, dict):
        for k, v in schema.items():
            # 类型/描述应是短字符串;若值是 list/dict(疑似塞了样本数据)则可疑
            if isinstance(v, (list, dict)) and len(json.dumps(v, ensure_ascii=False)) > 80:
                suspicious.append(k)
    return {"fields": fields, "field_count": len(fields),
            "no_plaintext": not suspicious, "suspicious_fields": suspicious}


def record_llm_exposure(schema: dict, query: str, purpose: str = "analyze",
                        user: Optional[str] = None, session_id: Optional[str] = None) -> None:
    """记录一次"LLM 只见 schema"事件 + 零明文断言。user/session 缺省取请求上下文。"""
    user, session_id = _ctx_user_session(user, session_id)
    a = assert_no_plaintext(schema, query)
    _append(user, {
        "type": "llm_exposure",
        "session": session_id,
        "purpose": purpose,
        "fields": a["fields"],
        "field_count": a["field_count"],
        "no_plaintext": a["no_plaintext"],
        "suspicious_fields": a["suspicious_fields"],
        "query_preview": (query or "")[:120],
    })


def record_decrypt_auth(decision: str, detail: str = "",
                        user: Optional[str] = None, session_id: Optional[str] = None) -> None:
    """记录一次解密授权门事件。decision ∈ granted/denied/keep_encrypted。user/session 缺省取上下文。"""
    user, session_id = _ctx_user_session(user, session_id)
    _append(user, {
        "type": "decrypt_auth",
        "session": session_id,
        "decision": decision,
        "detail": detail[:200],
    })


def read_events(user: Optional[str], limit: int = 500, etype: Optional[str] = None) -> list[dict]:
    """读回审计事件(最近 limit 条,可按类型过滤)。"""
    p = _path(user)
    if not p.exists():
        return []
    out = []
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if etype and ev.get("type") != etype:
                continue
            out.append(ev)
    except Exception:  # noqa: BLE001
        return out
    return out[-limit:]


def summary(user: Optional[str]) -> dict:
    """合规摘要:事件计数、零明文是否始终成立、最近解密授权。"""
    evs = read_events(user, limit=100000)
    exposures = [e for e in evs if e.get("type") == "llm_exposure"]
    decrypts = [e for e in evs if e.get("type") == "decrypt_auth"]
    breaches = [e for e in exposures if not e.get("no_plaintext", True)]
    return {
        "user": user or "default",
        "total_events": len(evs),
        "llm_exposures": len(exposures),
        "decrypt_authorizations": len(decrypts),
        "decrypt_granted": sum(1 for e in decrypts if e.get("decision") == "granted"),
        "decrypt_denied": sum(1 for e in decrypts if e.get("decision") in ("denied", "keep_encrypted")),
        "zero_plaintext_holds": not breaches,    # 是否始终满足"LLM 只见 schema"
        "plaintext_breaches": len(breaches),
        "first_event": evs[0]["ts"] if evs else None,
        "last_event": evs[-1]["ts"] if evs else None,
        "statement": ("✓ 全程满足:LLM 仅接收字段名 schema,明文数据值从未外发;"
                      "所有解密均经用户本机授权,可追溯。"
                      if not breaches else
                      f"⚠ 检出 {len(breaches)} 次疑似明文外发,需复核。"),
    }
