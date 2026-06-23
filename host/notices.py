"""
Admin 端站内信(只读通知 + 留痕)。

与客户端站内信同思路:消息只含**文字**(标题/摘要/时间/分级),**不带执行按钮**;
处理在各实时页面(用户 / LLM 配置 / 运维)里做。消息从主机已有的监控数据派生
(授权失效 / LLM 调用失败 / 服务被守护重启),读取时同步 —— 天然支持"补站内信"
(底层信号持久,主机恢复后下次同步即补出)。

存储:~/.agent-system/admin/notices.json(沙盒内,只含文字,无密钥/明文)。
"""
from __future__ import annotations

import json
import secrets
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

NOTICES_PATH = Path.home() / ".agent-system" / "admin" / "notices.json"

LEVELS = ("info", "warning", "critical")


@dataclass
class Notice:
    id: str
    key: str                 # 去重键(来源事件)→ 同一事件只一条
    level: str               # info / warning / critical
    title: str
    summary: str
    created_at: str
    read: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class NoticeStore:
    """主机端全局站内信(无 admin 账户体系,故不按用户分区)。"""

    def __init__(self, path: Optional[Path] = None):
        self._path = path or NOTICES_PATH
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._items: dict[str, Notice] = {}
        self._keys: set[str] = set()
        for d in self._read():
            try:
                n = Notice(**{k: d.get(k) for k in Notice.__dataclass_fields__ if k in d})
                self._items[n.id] = n
                self._keys.add(n.key)
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

    def seen_keys(self) -> set[str]:
        return set(self._keys)

    def add(self, *, key: str, level: str, title: str,
            summary: str, created_at: str = "") -> Optional[Notice]:
        with self._lock:
            if key in self._keys:
                return None
            nid = secrets.token_hex(6)
            while nid in self._items:
                nid = secrets.token_hex(6)
            n = Notice(
                id=nid, key=key,
                level=level if level in LEVELS else "info",
                title=title, summary=summary,
                created_at=created_at or datetime.now().isoformat(timespec="seconds"),
            )
            self._items[nid] = n
            self._keys.add(key)
            self._flush()
            return n

    def list(self, limit: int = 200) -> list[Notice]:
        out = list(self._items.values())
        out.sort(key=lambda n: n.created_at, reverse=True)
        return out[:limit]

    def unread_count(self) -> int:
        return sum(1 for n in self._items.values() if not n.read)

    def mark_all_read(self) -> None:
        with self._lock:
            changed = False
            for n in self._items.values():
                if not n.read:
                    n.read = True
                    changed = True
            if changed:
                self._flush()
