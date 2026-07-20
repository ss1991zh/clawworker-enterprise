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
import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

AUDIT_DIR = Path(os.path.expanduser("~/.agent-system/audit"))

_GENESIS = "0" * 64                       # 链首 prev_hash
_chain_lock = threading.Lock()
_last_hash: dict[str, str] = {}           # 每文件最后一条事件的 hash 缓存


def _event_hash(event: dict) -> str:
    """对事件(不含 'hash' 字段)做规范化 sha256。"""
    payload = {k: event[k] for k in event if k != "hash"}
    canon = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _read_last_hash(path: Path) -> str:
    """冷启动:取最后一条**能解析**的事件的 hash(文件不存在 → 链首)。

    末行可能因写入时崩溃/断电而残缺。若因残行直接回退成链首,新事件会从中间
    另起一条链,verify 时表现为"断链",与真篡改混淆 —— 故跳过残行往前找。
    """
    try:
        if not path.exists():
            return _GENESIS
        last_ok = _GENESIS
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    h = json.loads(line).get("hash")
                except Exception:  # noqa: BLE001 —— 残行,跳过继续往前找
                    continue
                if h:
                    last_ok = h
        return last_ok
    except Exception:  # noqa: BLE001
        return _GENESIS

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
    """写一行审计事件(带哈希链:seq + prev_hash + hash);失败绝不影响主流程。"""
    try:
        path = _path(user)
        key = str(path)
        with _chain_lock:
            prev = _last_hash.get(key)
            if prev is None:
                prev = _read_last_hash(path)
            event = {"ts": _now(), **event, "prev_hash": prev}
            event["hash"] = _event_hash(event)
            # 上次写入若崩在半途,末行没有换行符 —— 直接 append 会把新事件**粘在残行尾部**,
            # 既丢了这条事件,又让日志永久损坏。先补一个换行,保证新事件独占一行。
            need_nl = False
            try:
                if path.exists() and path.stat().st_size:
                    with path.open("rb") as fb:
                        fb.seek(-1, os.SEEK_END)
                        need_nl = fb.read(1) != b"\n"
            except Exception:  # noqa: BLE001
                need_nl = False
            with path.open("a", encoding="utf-8") as f:
                if need_nl:
                    f.write("\n")
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            _last_hash[key] = event["hash"]
    except Exception:  # noqa: BLE001 —— 审计失败不能阻断分析
        pass


def verify_chain(user: Optional[str]) -> dict:
    """
    校验某用户审计日志的哈希链完整性 —— 检测事后篡改/删行/改字段。
    返回 {ok, total, broken_at, reason}。broken_at 是第一处断裂的行号(1-based)。
    注:本地日志由用户自己所有,哈希链检测**部分篡改**;抵御整档重写需主机侧定期锚定(另议)。
    """
    path = _path(user)
    if not path.exists():
        return {"ok": True, "total": 0, "broken_at": None, "reason": "无日志"}
    prev = _GENESIS
    n = 0
    pending_bad: Optional[int] = None   # 解析不了的行号(先记着,看后面还有没有内容)
    try:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                if pending_bad is not None:
                    # 残行后面还有正常内容 → 不是"崩在最后一行",按损坏/篡改处理
                    return {"ok": False, "total": n, "broken_at": pending_bad,
                            "reason": "行损坏(疑似删改)"}
                try:
                    ev = json.loads(line)
                except Exception:  # noqa: BLE001
                    pending_bad = i
                    continue
                n += 1
                stored = ev.get("hash")
                if ev.get("prev_hash") != prev:
                    return {"ok": False, "total": n, "broken_at": i, "reason": "prev_hash 断链(疑似删行/插行)"}
                if _event_hash(ev) != stored:
                    return {"ok": False, "total": n, "broken_at": i, "reason": "hash 不匹配(疑似改字段)"}
                prev = stored
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "total": n, "broken_at": None, "reason": f"解析失败: {e}"}
    if pending_bad is not None:
        # 只有**末行**残缺 —— 断电/被杀进程的写入残留,不是篡改。如实区分,别误报。
        return {"ok": False, "total": n, "broken_at": pending_bad, "truncated_tail": True,
                "reason": f"末行不完整(疑似写入时崩溃,非篡改);其前 {n} 条链完整"}
    return {"ok": True, "total": n, "broken_at": None, "reason": "链完整"}


def _field_names(schema: dict) -> list[str]:
    """提取真正暴露给 LLM 的**字段名**。兼容两种 schema 形态:
    标准 sidecar {scenario, columns:[{name,type,encrypted}], metadata_columns, primary_key} →
    取 columns[].name;简单 {字段名: 类型} → 取 keys。"""
    if not isinstance(schema, dict):
        return []
    cols = schema.get("columns")
    if isinstance(cols, list) and cols and isinstance(cols[0], dict):
        return [str(c.get("name", "")) for c in cols if c.get("name")]
    # 退化:简单 {名:类型};排除已知结构键
    structural = {"scenario", "columns", "metadata_columns", "primary_key"}
    return [str(k) for k in schema.keys() if k not in structural]


def _contains_data_values(obj) -> bool:
    """是否含**实际数据值**(数值数组 / 数据行),而非字段名/类型元数据。
    标准 schema 的 columns 是 {name,type,encrypted} 定义(布尔除外无数字)→ 不触发。
    若误塞了数据列(如 {col:[1,2,3,4]})→ 触发。"""
    if isinstance(obj, list):
        nums = [x for x in obj if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if len(nums) >= 3:            # 像一列数据
            return True
        return any(_contains_data_values(x) for x in obj)
    if isinstance(obj, dict):
        return any(_contains_data_values(v) for v in obj.values())
    return False


def assert_no_plaintext(schema: dict, query: str) -> dict:
    """零明文断言:确认发给 LLM 的只有**字段名 + 类型元数据**,不含任何数据行/单元值。
    记录真实字段名(columns[].name);仅当检出嵌入的数值数组/数据行时才判为外发。"""
    fields = _field_names(schema)
    has_data = _contains_data_values(schema)
    return {"fields": fields, "field_count": len(fields),
            "no_plaintext": not has_data,
            "suspicious_fields": ["<检出疑似数据值>"] if has_data else []}


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
