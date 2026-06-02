"""
v4 会话持久化 — 简化:不绑密文。

用户每条消息可以带 attached_cipher,会被记到 message.attached_cipher。
新建会话不需要预先选密文,LLM 在 schema 缺失时会追问。
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
    # user-only:用户在这条消息附带的密文文件(可为空,自动沿用上一条 user 消息的)
    attached_cipher: str = ""
    # assistant-only:
    summary: str = ""
    excel_path: str = ""
    excel_name: str = ""
    error: str = ""
    skill_calls: list[str] = field(default_factory=list)   # 跑了哪些 skill 名
    steps: list[dict[str, Any]] = field(default_factory=list)
    status: str = "done"                       # pending / running / done / failed / needs_cipher
    duration_sec: float = 0.0
    used_cipher: str = ""                      # assistant 实际用了哪份 cipher
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            id=d.get("id", secrets.token_hex(6)),
            role=d.get("role", "user"),
            content=d.get("content", ""),
            attached_cipher=d.get("attached_cipher", "") or d.get("attached_cipher_path", ""),
            summary=d.get("summary", ""),
            excel_path=d.get("excel_path", ""),
            excel_name=d.get("excel_name", ""),
            error=d.get("error", ""),
            skill_calls=list(d.get("skill_calls", []) or []),
            steps=list(d.get("steps", []) or []),
            status=d.get("status", "done"),
            duration_sec=float(d.get("duration_sec", 0.0) or 0.0),
            used_cipher=d.get("used_cipher", ""),
            created_at=d.get("created_at", datetime.now().isoformat(timespec="seconds")),
        )


@dataclass
class ChatSession:
    id: str
    title: str
    username: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
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
        )
        sess.messages = [Message.from_dict(m) for m in d.get("messages", []) or []]
        return sess

    def last_attached_cipher(self) -> str:
        """从消息历史里找最近一次 user 消息附带的密文(用于"追问沿用")。"""
        for m in reversed(self.messages):
            if m.role == "user" and m.attached_cipher:
                return m.attached_cipher
        return ""


class SessionStore:
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

    def rename(self, sid: str, title: str) -> None:
        sess = self._sessions.get(sid)
        if not sess:
            return
        with self._lock:
            sess.title = title[:60]
            self._save(sess)
