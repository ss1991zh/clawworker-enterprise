"""
Admin 只读查看「用户会话记录 + 定时任务」。

本部署 host 与 client 同机、共享 ~/.agent-system/,故 host 可**只读**客户端的会话/任务文件。
严守边界:
  · 只暴露**文字与元信息**(用户名、标题、问题、零明文摘要、附件**名**、时间、任务定义);
  · **绝不**读取/暴露附件内容、密文文件、明文 Excel —— 只显示其文件名;
  · admin 的"清除记录显示"是 host 侧的**隐藏集**(只影响 admin 视图),不动用户任何数据。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

_BASE = Path.home() / ".agent-system"
SESSIONS_DIR = _BASE / "sessions"
TASKS_FILE = _BASE / "scheduler" / "tasks.json"
HIDDEN_FILE = _BASE / "admin" / "hidden_records.json"


# ---- admin 侧"隐藏集"(只影响 admin 视图)----

def _load_hidden() -> set[str]:
    try:
        return set(json.loads(HIDDEN_FILE.read_text(encoding="utf-8")))
    except (FileNotFoundError, ValueError, TypeError):
        return set()


def _save_hidden(ids: set[str]) -> None:
    HIDDEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = HIDDEN_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HIDDEN_FILE)


def hide(rid: str) -> None:
    ids = _load_hidden(); ids.add(rid); _save_hidden(ids)


def unhide(rid: str) -> None:
    ids = _load_hidden()
    if rid in ids:
        ids.discard(rid); _save_hidden(ids)


def hidden_ids() -> set[str]:
    return _load_hidden()


# ---- 只读解析 ----

def _name_only(path: str) -> str:
    """文件路径 → 仅文件名(绝不暴露完整路径或内容)。"""
    return Path(path).name if path else ""


def _clean_title(t: str) -> str:
    """去掉客户端定时会话标题里的 ⏰ 前缀(admin 用扁平图标区分,不用 emoji)。"""
    t = (t or "").strip()
    if t.startswith("⏰"):
        t = t[1:].strip()
    return t or "未命名会话"


def _read_session_files() -> list[dict]:
    out = []
    if not SESSIONS_DIR.is_dir():
        return out
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            out.append(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, OSError):
            continue
    return out


def _msg_attach_names(m: dict) -> list[str]:
    """一条消息涉及的附件名(输入密文 + 文本附件),只取名,不取内容/路径。"""
    names: list[str] = []
    c = _name_only(m.get("attached_cipher", "") or m.get("attached_cipher_path", ""))
    if c:
        names.append(c)
    for n in (m.get("text_attachment_names") or []):
        if n:
            names.append(str(n))
    return names


def list_sessions() -> list[dict]:
    """所有用户的会话(汇总,不含逐条消息);按最后活动倒序。"""
    hid = hidden_ids()
    rows = []
    for d in _read_session_files():
        sid = d.get("id", "")
        if not sid:
            continue
        msgs = d.get("messages", []) or []
        attach: list[str] = []
        for m in msgs:
            attach.extend(_msg_attach_names(m))
        rows.append({
            "id": sid,
            "username": d.get("username", ""),
            "title": _clean_title(d.get("title", "")),
            "kind": d.get("kind", "normal") or "normal",
            "task_id": d.get("task_id", "") or "",
            "msg_count": len(msgs),
            "attach_names": sorted(set(n for n in attach if n)),
            "updated_at": d.get("updated_at", "") or d.get("created_at", ""),
            "hidden": (f"session:{sid}" in hid),
        })
    rows.sort(key=lambda r: r["updated_at"], reverse=True)
    return rows


def get_session(sid: str) -> Optional[dict]:
    """单个会话的逐条消息(用于详情页):只给文字 + 附件名 + 时间,无内容/路径。"""
    for d in _read_session_files():
        if d.get("id") != sid:
            continue
        msgs = []
        for m in (d.get("messages", []) or []):
            role = m.get("role", "")
            # 用户/系统事件显示其文本;助手显示零明文摘要(没有则给状态占位)
            text = (m.get("content", "") if role in ("user", "event")
                    else (m.get("summary", "") or ""))
            msgs.append({
                "role": role,
                "event_kind": m.get("event_kind", ""),
                "text": text,
                "remediation_note": m.get("remediation_note", ""),
                "attach_names": _msg_attach_names(m),
                "output_name": _name_only(m.get("excel_name", "") or "") or _name_only(m.get("enc_excel_name", "") or ""),
                "status": m.get("status", "done"),
                "error": (m.get("error", "") or "")[:300],
                "created_at": m.get("created_at", ""),
            })
        return {
            "id": sid,
            "username": d.get("username", ""),
            "title": _clean_title(d.get("title", "")),
            "kind": d.get("kind", "normal") or "normal",
            "created_at": d.get("created_at", ""),
            "updated_at": d.get("updated_at", ""),
            "messages": msgs,
        }
    return None


def list_users() -> list[dict]:
    """按用户聚合:可见会话数 / 可见任务数 / 已隐藏数 / 最后活动。按最后活动倒序。"""
    agg: dict[str, dict] = {}

    def _u(name: str) -> dict:
        name = name or "(未知用户)"
        return agg.setdefault(name, {
            "username": name, "sessions": 0, "tasks": 0,
            "hidden_sessions": 0, "hidden_tasks": 0, "last": "",
        })

    for s in list_sessions():
        u = _u(s["username"])
        if s["hidden"]:
            u["hidden_sessions"] += 1
        else:
            u["sessions"] += 1
            if s["updated_at"] > u["last"]:
                u["last"] = s["updated_at"]
    for t in list_tasks():
        u = _u(t["username"])
        if t["hidden"]:
            u["hidden_tasks"] += 1
        else:
            u["tasks"] += 1
    out = list(agg.values())
    out.sort(key=lambda x: (x["last"], x["sessions"]), reverse=True)
    return out


def list_tasks() -> list[dict]:
    """所有用户的定时任务(只读定义,不含数据)。"""
    hid = hidden_ids()
    try:
        raw = json.loads(TASKS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return []
    rows = []
    for t in raw:
        tid = t.get("id", "")
        if not tid:
            continue
        rows.append({
            "id": tid,
            "username": t.get("username", ""),
            "name": t.get("name", "") or "未命名任务",
            "question": t.get("question", "") or "",
            "schedule": (t.get("cron_readable", "") or t.get("schedule_kind", "") or "—"),
            "enabled": bool(t.get("enabled", False)),
            "needs_approval": bool(t.get("needs_approval", t.get("source_folder") or t.get("cipher_path"))),
            "source_name": _name_only(t.get("source_folder", "") or t.get("cipher_path", "")),
            "last_fired": t.get("last_fired", "") or "",
            "hidden": (f"task:{tid}" in hid),
        })
    rows.sort(key=lambda r: r["username"])
    return rows
