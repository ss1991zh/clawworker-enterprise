"""
客户端站内信(只读通知 + 留痕)。

设计要点(与企业版定位一致):
  · 消息只含**文字**:标题 / 摘要 / 时间 / 分级(info|warning|critical),**不带执行按钮**;
    真正的处理在「定时任务管理」等实时页面里做,消息只负责"让你知道"。
  · 消息**从既有持久 store 派生**(漏跑 / 待解密 / 会话失败),读取时同步:
    底层记录在服务中断时依然落盘,服务恢复后下次同步即补出对应站内信 —— 天然支持"补站内信"。
  · 每条消息有去重 key(来源事件 id),同一事件只生成一条;已读状态本地持久。

存储:~/.agent-system/scheduler/notices.json(沙盒内,与其它定时任务数据同目录)。
"""
from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from client.webui.scheduler import SCHED_DIR

NOTICES_PATH = SCHED_DIR / "notices.json"

LEVELS = ("info", "warning", "critical")


@dataclass
class Notice:
    id: str
    username: str
    key: str                 # 去重键(来源事件 id)→ 同一事件只一条
    level: str               # info / warning / critical
    title: str
    summary: str             # 可较详细的纯文字摘要
    created_at: str          # 事件发生时间(非同步时间),用于按时间排序
    read: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class NoticeStore:
    def __init__(self, path: Optional[Path] = None):
        self._path = path or NOTICES_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._items: dict[str, Notice] = {}
        self._keys: set[tuple[str, str]] = set()   # (username, key) 去重
        for d in self._read():
            try:
                n = Notice(**{k: d.get(k) for k in Notice.__dataclass_fields__ if k in d})
                self._items[n.id] = n
                self._keys.add((n.username, n.key))
            except Exception:
                continue

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _flush(self) -> None:
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps([n.to_dict() for n in self._items.values()],
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass

    def seen_keys(self, username: str) -> set[str]:
        return {k for (u, k) in self._keys if u == username}

    def add(self, *, username: str, key: str, level: str,
            title: str, summary: str, created_at: str = "") -> Optional[Notice]:
        """新增一条(同 (username,key) 已存在则跳过,返回 None)。"""
        with self._lock:
            if (username, key) in self._keys:
                return None
            nid = secrets.token_hex(6)
            while nid in self._items:
                nid = secrets.token_hex(6)
            n = Notice(
                id=nid, username=username, key=key,
                level=level if level in LEVELS else "info",
                title=title, summary=summary,
                created_at=created_at or datetime.now().isoformat(timespec="seconds"),
            )
            self._items[nid] = n
            self._keys.add((username, key))
            self._flush()
            return n

    def list_for(self, username: str, limit: int = 200) -> list[Notice]:
        out = [n for n in self._items.values() if n.username == username]
        out.sort(key=lambda n: n.created_at, reverse=True)
        return out[:limit]

    def unread_count(self, username: str) -> int:
        return sum(1 for n in self._items.values()
                   if n.username == username and not n.read)

    def mark_all_read(self, username: str) -> None:
        with self._lock:
            changed = False
            for n in self._items.values():
                if n.username == username and not n.read:
                    n.read = True
                    changed = True
            if changed:
                self._flush()
