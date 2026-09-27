"""本地密文文件列表、预览与删除路由。"""
from __future__ import annotations

import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, HTTPException, UploadFile


MAX_DATA_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_TEXT_UPLOAD_BYTES = 20 * 1024 * 1024
_DATA_EXTS = {".csv", ".xlsx", ".xls"}


def _owned_ciphertext(storage: Any, name: str) -> Path:
    root = Path(storage.ciphertext_dir).resolve()
    target = (root / name).resolve()
    if target == root or not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(404, "文件不存在或路径非法")
    return target


def build_files_router(
    *,
    storage: Any,
    ingest_plaintext_path: Callable[[Path, str], dict[str, Any]],
    extract_text: Callable[[Path], str],
    is_logged_in: Callable[[], bool],
    need_login: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/files", tags=["cipher-files"])

    async def save_upload(upload: UploadFile, *, limit: int, allowed: set[str]) -> tuple[Path, str]:
        original_name = Path(upload.filename or "attachment").name
        suffix = Path(original_name).suffix.lower()
        if suffix not in allowed:
            raise HTTPException(400, f"不支持的文件格式:{suffix or '无后缀'}")
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp_path = Path(tmp.name)
        size = 0
        try:
            with tmp:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"文件过大，最大允许 {limit // 1024 // 1024} MB")
                    tmp.write(chunk)
            if size == 0:
                raise HTTPException(400, "文件内容为空")
            return tmp_path, original_name
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    @router.post("/upload")
    async def upload_and_encrypt(raw_file: UploadFile = File(...)):
        if not is_logged_in():
            return need_login()
        tmp_path, original_name = await save_upload(
            raw_file, limit=MAX_DATA_UPLOAD_BYTES, allowed=_DATA_EXTS,
        )
        try:
            return ingest_plaintext_path(tmp_path, original_name)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 —— 将本机解析/密态库错误转换为可读提示
            raise HTTPException(422, f"加密失败:{type(exc).__name__}: {exc}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)

    @router.post("/text_extract")
    async def extract_text_attachment(raw_file: UploadFile = File(...)):
        if not is_logged_in():
            return need_login()
        # 由提取模块维护允许格式；从文件名先做同一口径的校验。
        allowed = {
            ".txt", ".md", ".markdown", ".rst", ".log", ".text",
            ".docx", ".pdf", ".rtf", ".html", ".htm", ".json", ".yml", ".yaml",
        }
        tmp_path, original_name = await save_upload(
            raw_file, limit=MAX_TEXT_UPLOAD_BYTES, allowed=allowed,
        )
        try:
            content = extract_text(tmp_path)
            return {"name": original_name, "content": content, "chars": len(content)}
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(422, f"文本读取失败:{type(exc).__name__}: {exc}") from exc
        finally:
            tmp_path.unlink(missing_ok=True)

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
