"""崩溃安全的同目录原子文件写入。"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from collections.abc import Callable


def _temporary_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _temporary_path(path)
    try:
        with temp_path.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _temporary_path(path)
    try:
        with temp_path.open("w", encoding=encoding, newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2))


def atomic_write_file(path: Path, writer: Callable[[Path], Any]) -> None:
    """供 Excel 等第三方 writer 使用：先生成完整临时文件，再替换目标。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _temporary_path(path)
    try:
        writer(temp_path)
        # Windows CRT 的 fsync 需要可写文件描述符；只读句柄会返回 EBADF。
        with temp_path.open("rb+") as stream:
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
