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
    role: str                                 # user / assistant / event(系统事件:漏跑/忽略/补救)
    content: str = ""
    # event-only:事件类型 missed(漏跑) / dismissed(已忽略) / remediated(已补救),决定展示样式
    event_kind: str = ""
    # user-only:用户在这条消息附带的密文文件(可为空,自动沿用上一条 user 消息的)
    attached_cipher: str = ""
    # user-only:本条消息附带的明文文本附件名(供前端 chip 展示;内容不持久化)
    text_attachment_names: list[str] = field(default_factory=list)
    # assistant-only:
    summary: str = ""
    excel_path: str = ""            # 明文 Excel(decrypt 时;或事后解密后)
    excel_name: str = ""
    enc_excel_path: str = ""        # 密文 Excel(加密版)
    enc_excel_name: str = ""
    can_decrypt: bool = False       # 保留密文场景:可点「解密」事后解出明文
    dec_run_id: str = ""            # 事后解密用的加密暂存 run_id(沙盒)
    dec_stem: str = ""              # 事后解密输出文件名 stem
    error: str = ""
    skill_calls: list[str] = field(default_factory=list)   # 跑了哪些 skill 名
    steps: list[dict[str, Any]] = field(default_factory=list)
    wizard: dict[str, Any] = field(default_factory=dict)   # 非空=该助手消息触发"创建定时任务"向导(预填槽位)
    clarify: dict[str, Any] = field(default_factory=dict)  # 非空=意图有歧义,需用户先选择(question + options)
    status: str = "done"                       # pending / running / done / failed / needs_cipher
    duration_sec: float = 0.0
    tokens: int = 0                            # 本轮所有 LLM 调用的 token 用量合计
    remediation_note: str = ""                 # assistant-only:本轮是漏跑补救时,附在执行时间下方的说明
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
            event_kind=d.get("event_kind", ""),
            attached_cipher=d.get("attached_cipher", "") or d.get("attached_cipher_path", ""),
            text_attachment_names=list(d.get("text_attachment_names", []) or []),
            summary=d.get("summary", ""),
            excel_path=d.get("excel_path", ""),
            excel_name=d.get("excel_name", ""),
            enc_excel_path=d.get("enc_excel_path", ""),
            enc_excel_name=d.get("enc_excel_name", ""),
            can_decrypt=bool(d.get("can_decrypt", False)),
            dec_run_id=d.get("dec_run_id", ""),
            dec_stem=d.get("dec_stem", ""),
            error=d.get("error", ""),
            skill_calls=list(d.get("skill_calls", []) or []),
            steps=list(d.get("steps", []) or []),
            wizard=dict(d.get("wizard", {}) or {}),
            clarify=dict(d.get("clarify", {}) or {}),
            status=d.get("status", "done"),
            duration_sec=float(d.get("duration_sec", 0.0) or 0.0),
            tokens=int(d.get("tokens", 0) or 0),
            remediation_note=d.get("remediation_note", ""),
            used_cipher=d.get("used_cipher", ""),
            created_at=d.get("created_at", datetime.now().isoformat(timespec="seconds")),
        )


@dataclass
class ChatSession:
    id: str
    title: str
    username: str
    kind: str = "normal"            # normal=普通会话 / scheduled=定时任务专用会话
    task_id: str = ""               # kind=scheduled 时关联的任务 id(供分组/跳转)
    # 定时任务会话从侧栏"删除"=软隐藏(不毁数据):任务仍在跑、历次运行继续累积,
    # 「查看会话」会重新取消隐藏并展示全部已运行内容。删除任务时才真正清除。
    hidden: bool = False
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
            kind=d.get("kind", "normal") or "normal",
            task_id=d.get("task_id", "") or "",
            hidden=bool(d.get("hidden", False)),
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


def _fold_remediation_events(sess: "ChatSession") -> bool:
    """一次性迁移:把旧版独立的「补救」事件消息(role=event, event_kind=remediated)
    折叠成所属那一轮 assistant 消息的 remediation_note,并删除该独立事件。
    幂等:无独立补救事件时返回 False。漏跑(missed)/忽略(dismissed)事件保持不变。"""
    msgs = sess.messages
    drop: set[int] = set()
    for i, m in enumerate(msgs):
        if getattr(m, "role", "") != "event" or getattr(m, "event_kind", "") != "remediated":
            continue
        target = None
        # 旧顺序 [补救事件][user][assistant] → 取后面最近的 assistant
        for j in range(i + 1, min(i + 3, len(msgs))):
            if msgs[j].role == "assistant":
                target = msgs[j]
                break
        # 兼容 [user][assistant][补救事件] → 取前面最近的 assistant
        if target is None:
            for j in range(i - 1, max(i - 3, -1), -1):
                if msgs[j].role == "assistant":
                    target = msgs[j]
                    break
        if target is not None and not getattr(target, "remediation_note", ""):
            target.remediation_note = m.content
        drop.add(i)
    if not drop:
        return False
    sess.messages = [m for k, m in enumerate(msgs) if k not in drop]
    return True


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
                changed = _fold_remediation_events(sess)   # 旧版独立"补救"事件 → 折进所属轮
                self._sessions[sess.id] = sess
                if changed:
                    self._save(sess)
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

    def create(self, *, username: str, title: str = "新会话",
               kind: str = "normal", task_id: str = "") -> ChatSession:
        with self._lock:
            sid = secrets.token_hex(6)
            while sid in self._sessions:
                sid = secrets.token_hex(6)
            sess = ChatSession(id=sid, title=title, username=username,
                               kind=kind, task_id=task_id)
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

    def set_hidden(self, sid: str, hidden: bool) -> bool:
        """软隐藏/恢复(定时任务会话从侧栏移除但保留全部数据)。"""
        sess = self._sessions.get(sid)
        if not sess:
            return False
        with self._lock:
            sess.hidden = bool(hidden)
            self._save(sess)
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
