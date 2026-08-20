"""本地密文文件列表、预览与删除路由。"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, HTTPException


def _owned_ciphertext(storage: Any, name: str) -> Path:
    root = Path(storage.ciphertext_dir).resolve()
    target = (root / name).resolve()
    if target == root or not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "文件不存在或路径非法")
    return target


def build_files_router(
    *,
    storage: Any,
    is_logged_in: Callable[[], bool],
    need_login: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/files", tags=["cipher-files"])

    @router.get("")
    def list_files():
        if not is_logged_in():
            return need_login()
        try:
            paths = storage.list_ciphertexts()
        except Exception:
            paths = []
        output = []
        for path in paths:
            if path.name.endswith((".meta.csv", ".schema.json")):
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            output.append({
                "name": path.name,
                "path": str(path),
                "size_kb": round(stat.st_size / 1024, 1),
                "mtime": datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds"),
                "has_meta": path.with_suffix(path.suffix + ".meta.csv").exists(),
            })
        output.sort(key=lambda item: item["mtime"], reverse=True)
        return output

    @router.delete("/{name}")
    def delete_file(name: str):
        if not is_logged_in():
            return need_login()
        target = _owned_ciphertext(storage, name)
        target.unlink()
        target.with_suffix(target.suffix + ".meta.csv").unlink(missing_ok=True)
        target.with_suffix(target.suffix + ".schema.json").unlink(missing_ok=True)
        return {"ok": True}

    @router.get("/{name}/preview")
    def preview_file(name: str):
        if not is_logged_in():
            return need_login()
        target = _owned_ciphertext(storage, name)
        info: dict[str, Any] = {
            "name": target.name,
            "path": str(target),
            "size_kb": round(target.stat().st_size / 1024, 1),
        }
        meta_path = target.with_suffix(target.suffix + ".meta.csv")
        if meta_path.exists():
            try:
                import pandas as pd

                frame = pd.read_csv(meta_path)
                info["meta_columns"] = list(frame.columns)
                info["meta_row_count"] = len(frame)
                info["meta_preview"] = frame.head(8).fillna("").astype(str).values.tolist()
            except Exception as exc:  # noqa: BLE001
                info["meta_error"] = str(exc)
        else:
            info.update({"meta_columns": [], "meta_preview": [], "meta_row_count": 0})
        schema_path = target.with_suffix(target.suffix + ".schema.json")
        if schema_path.exists():
            try:
                info["schema"] = json.loads(schema_path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001
                info["schema_error"] = str(exc)
        else:
            info["schema"] = None
        return info

    return router
