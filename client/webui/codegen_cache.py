"""定时任务代码固化缓存，含原子写入、容量与过期清理。"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Optional

from client.webui.state import atomic_write_json


from shared.paths import SCHEDULER_DIR

CACHE_DIR = SCHEDULER_DIR / "codegen_cache"
MAX_FILES = 200
MAX_AGE_SECONDS = 90 * 24 * 60 * 60


def signature(effective_query: str, schema: dict) -> str:
    columns = ",".join(sorted(
        str(column.get("name", "")) for column in (schema or {}).get("columns", [])
    ))
    return hashlib.sha256(f"{effective_query}|{columns}".encode("utf-8")).hexdigest()[:16]


def load(cache_key: str, expected_signature: str) -> Optional[dict]:
    path = CACHE_DIR / f"{cache_key}.json"
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("sig") != expected_signature or not data.get("code"):
            return None
        return {"code": data["code"], "summary": data.get("summary") or "",
                "lazy_waived": bool(data.get("lazy_waived"))}
    except (OSError, ValueError, TypeError):
        return None


def save(cache_key: str, sig: str, code: str, summary: str,
         lazy_waived: bool = False) -> None:
    try:
        atomic_write_json(
            CACHE_DIR / f"{cache_key}.json",
            {"sig": sig, "code": code, "summary": summary,
             "lazy_waived": lazy_waived},
        )
        cleanup()
    except OSError:
        pass


def cleanup(*, now: Optional[float] = None) -> int:
    current = time.time() if now is None else now
    try:
        files = sorted(CACHE_DIR.glob("*.json"),
                       key=lambda path: path.stat().st_mtime, reverse=True)
    except OSError:
        return 0
    removed = 0
    for index, path in enumerate(files):
        try:
            if path.stat().st_mtime < current - MAX_AGE_SECONDS or index >= MAX_FILES:
                path.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def delete(cache_key: str) -> None:
    try:
        (CACHE_DIR / f"{cache_key}.json").unlink(missing_ok=True)
    except OSError:
        pass
