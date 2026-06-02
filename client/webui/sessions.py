"""
会话(Chat Session)持久化 —— ChatGPT 风格客户端 UI 的状态层。

每个 ChatSession:
- 多轮消息(user / assistant)
- 绑定一份"密文文件 + schema"作为会话上下文
- JSON 落盘到 ~/.agent-system/sessions/{id}.json,重启后还在
"""

from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


SESSIONS_DIR = Path.home() / ".agent-system" / "sessions"


@dataclass
class Message:
    id: str
    role: str                                 # user / assistant
    content: str = ""
    attachments: list[dict[str, Any]] = field(default_factory=list)
    # assistant-only:
    summary: str = ""                          # workflow 总结(已过滤)
    excel_path: str = ""                       # 生成的 Excel 路径
    excel_name: str = ""                       # Excel 文件名
    error: str = ""
    scenario: str = ""
    plan_summary: str = ""
    # 每一步执行追踪 — 给"计算追踪"折叠面板用
    # [{kind: think|call|result|error, label: str, detail: str(optional)}]
    steps: list[dict[str, Any]] = field(default_factory=list)
    status: str = "done"                       # pending / running / done / failed
    duration_sec: float = 0.0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            id=d.get("id", secrets.token_hex(6)),
            role=d.get("role", "user"),
            content=d.get("content", ""),
            attachments=list(d.get("attachments", []) or []),
            summary=d.get("summary", ""),
            excel_path=d.get("excel_path", ""),
            excel_name=d.get("excel_name", ""),
            error=d.get("error", ""),
            scenario=d.get("scenario", ""),
            plan_summary=d.get("plan_summary", ""),
            steps=list(d.get("steps", []) or []),
            status=d.get("status", "done"),
            duration_sec=float(d.get("duration_sec", 0.0) or 0.0),
            created_at=d.get("created_at", datetime.now().isoformat(timespec="seconds")),
        )


@dataclass
class ChatSession:
    id: str
    title: str
    username: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    # 会话上下文:绑定的密文 + schema(用户切换就更新)
    context_ciphertext: str = ""
    context_schema: str = ""
    messages: list[Message] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["messages"] = [m.to_dict() if isinstance(m, Message) else m for m in self.messages]
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ChatSession":
        sess = cls(
            id=d["id"],
            title=d.get("title", "新会话"),
            username=d.get("username", ""),
            created_at=d.get("created_at", datetime.now().isoformat(timespec="seconds")),
            updated_at=d.get("updated_at", datetime.now().isoformat(timespec="seconds")),
            context_ciphertext=d.get("context_ciphertext", ""),
            context_schema=d.get("context_schema", ""),
        )
        sess.messages = [Message.from_dict(m) for m in d.get("messages", []) or []]
        return sess


class SessionStore:
    """ChatSession 持久化(每会话 1 个 JSON 文件)。"""

    def __init__(self, root: Optional[Path] = None):
        self._root = root or SESSIONS_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._sessions: dict[str, ChatSession] = {}
        self._load_all()

    def _path(self, sid: str) -> Path:
        return self._root / f"{sid}.json"

    def _load_all(self) -> None:
        for p in sorted(self._root.glob("*.json")):
            try:
                sess = ChatSession.from_dict(json.loads(p.read_text(encoding="utf-8")))
                # 运行中的 assistant 消息在进程重启后视为失败
                for m in sess.messages:
                    if m.role == "assistant" and m.status in ("pending", "running"):
                        m.status = "failed"
                        m.error = "主进程重启,任务中断"
                self._sessions[sess.id] = sess
            except Exception:
                continue

    def _save(self, sess: ChatSession) -> None:
        try:
            self._path(sess.id).write_text(
                json.dumps(sess.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    # ----- CRUD -----
    def list_for(self, username: str) -> list[ChatSession]:
        out = [s for s in self._sessions.values() if s.username == username]
        out.sort(key=lambda s: s.updated_at, reverse=True)
        return out

    def get(self, sid: str) -> Optional[ChatSession]:
        return self._sessions.get(sid)

    def create(self, *, username: str, title: str = "新会话") -> ChatSession:
        with self._lock:
            sid = secrets.token_hex(6)
            while sid in self._sessions:
                sid = secrets.token_hex(6)
            sess = ChatSession(id=sid, title=title, username=username)
            self._sessions[sid] = sess
            self._save(sess)
            return sess

    def delete(self, sid: str) -> bool:
        with self._lock:
            sess = self._sessions.pop(sid, None)
            if not sess:
                return False
            try:
                self._path(sid).unlink(missing_ok=True)
            except Exception:
                pass
            return True

    def append_message(self, sid: str, message: Message) -> None:
        sess = self._sessions.get(sid)
        if not sess:
            return
        with self._lock:
            sess.messages.append(message)
            sess.updated_at = datetime.now().isoformat(timespec="seconds")
            # 用首条用户消息做标题(若仍是"新会话")
            if sess.title in ("新会话", "") and message.role == "user" and message.content:
                sess.title = message.content[:40]
            self._save(sess)

    def update_message(self, sid: str, mid: str, **patch: Any) -> Optional[Message]:
        sess = self._sessions.get(sid)
        if not sess:
            return None
        with self._lock:
            for m in sess.messages:
                if m.id == mid:
                    for k, v in patch.items():
                        if hasattr(m, k):
                            setattr(m, k, v)
                    sess.updated_at = datetime.now().isoformat(timespec="seconds")
                    self._save(sess)
                    return m
        return None

    def set_context(self, sid: str, *, ciphertext: Optional[str] = None,
                    schema: Optional[str] = None) -> None:
        sess = self._sessions.get(sid)
        if not sess:
            return
        with self._lock:
            if ciphertext is not None:
                sess.context_ciphertext = ciphertext
            if schema is not None:
                sess.context_schema = schema
            self._save(sess)

    def rename(self, sid: str, title: str) -> None:
        sess = self._sessions.get(sid)
        if not sess:
            return
        with self._lock:
            sess.title = title[:60]
            self._save(sess)
