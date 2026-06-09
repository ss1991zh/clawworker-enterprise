"""
v4 客户端 Web UI 主入口 — skill-only 架构,无 LangGraph。

路由极简:
  /login              登录页
  /                   chat 主页(需要登录)
  /api/me             当前用户信息
  /api/config         本地配置 get/set
  /api/keys           密钥状态 + 上传
  /api/keys/fetch_auth  从主机拉证书副本
  /api/files          密文文件列表 / 上传 / 删除 / preview
  /api/sessions       会话 CRUD
  /api/sessions/{sid}/messages         发送消息(走 pipeline.ask)+ 拉历史
  /api/sessions/{sid}/messages/{mid}   轮询单条 assistant 消息
  /api/excel/download                  下载 ~/Downloads/ 下的 xlsx
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from client.keystore import Keystore
from client.local_storage import LocalStorage
from client.tools.crypto import ZFHE
from client.webui import pipeline as pipeline_mod
from client.webui import text_extract
from client.webui.sessions import ChatSession, Message, SessionStore
from client.webui.scheduler import (
    EncryptedResultStore,
    HistoryStore,
    PendingStore,
    Scheduler,
    TaskStore,
)
from client.webui.skills_store import (
    CustomSkillStore,
    build_custom_skills_prompt_block,
    builtin_skills,
)
from shared.prompts import load_system_prompt

# ----------------------------------------------------------------------------
# 路径 / 配置
# ----------------------------------------------------------------------------

_WEBUI_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEBUI_DIR / "templates"))

APP_DATA_DIR = Path.home() / ".agent-system"
CLIENT_CONFIG_FILE = APP_DATA_DIR / "client-config.json"

_lock = threading.Lock()
_session_state: dict[str, Any] = {"host_url": "", "username": "", "token": "", "expires_at": ""}

# 用户取消标记 —— pipeline 线程在检查点读取此 set
_cancelled_msgs: set[str] = set()
_cancel_lock = threading.Lock()

# 解密授权门(Human-in-the-Loop / HITL):
#   mid → "decrypt" / "keep_encrypted" / "cancel"
#   pipeline 线程阻塞等待 _decrypt_events[mid].set()
_decrypt_decisions: dict[str, str] = {}
_decrypt_events: dict[str, threading.Event] = {}
_decrypt_lock = threading.Lock()


def _load_config() -> dict[str, Any]:
    defaults = {
        "host_url": "http://127.0.0.1:8443",
        "backend": "real",
    }
    if CLIENT_CONFIG_FILE.exists():
        try:
            return {**defaults, **json.loads(CLIENT_CONFIG_FILE.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return defaults


def _save_config(cfg: dict[str, Any]) -> None:
    CLIENT_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLIENT_CONFIG_FILE.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_config = _load_config()
_storage = LocalStorage()
_keystore = Keystore()
_sessions = SessionStore()
_custom_skills = CustomSkillStore()
_task_store = TaskStore()
_pending_store = PendingStore()
_run_history = HistoryStore()
_enc_results = EncryptedResultStore()


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------

app = FastAPI(title="agent-system client", version="0.4.0")
app.mount("/static", StaticFiles(directory=str(_WEBUI_DIR / "static")), name="static")


def _is_logged_in() -> bool:
    return bool(_session_state.get("token") and _session_state.get("username"))


def _need_login() -> JSONResponse:
    return JSONResponse({"error": "not_logged_in"}, status_code=401)


def _clear_local_session() -> None:
    with _lock:
        _session_state.update({"host_url": "", "username": "", "token": "", "expires_at": ""})


def _flash_redirect(url: str, *messages: tuple[str, str]) -> RedirectResponse:
    if messages:
        parts = [f"_flash={quote(f'{c}|{m}', safe='')}" for c, m in messages]
        url = f"{url}{'&' if '?' in url else '?'}{'&'.join(parts)}"
    return RedirectResponse(url, status_code=303)


def _pop_messages(request: Request) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for val in parse_qs(request.url.query).get("_flash", []):
        if "|" in val:
            cat, msg = val.split("|", 1)
            out.append((cat, msg))
    return out


def _asset_version() -> str:
    try:
        mtimes = [
            (_WEBUI_DIR / "static" / "app.js").stat().st_mtime,
            (_WEBUI_DIR / "static" / "app.css").stat().st_mtime,
        ]
        return str(int(max(mtimes)))
    except OSError:
        return "0"


# ----------------------------------------------------------------------------
# 登录 / 主页
# ----------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _is_logged_in():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "index.html",
        {"username": _session_state["username"], "asset_ver": _asset_version()},
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _is_logged_in():
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html",
        {
            "default_host": _config.get("host_url", ""),
            "messages": _pop_messages(request),
            "asset_ver": _asset_version(),
        },
    )


@app.post("/login")
def login_submit(
    request: Request,
    host_url: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    host_url = host_url.rstrip("/")
    try:
        r = httpx.post(
            f"{host_url}/auth/login",
            json={"username": username, "password": password},
            timeout=15,
        )
    except httpx.HTTPError as e:
        return _flash_redirect("/login", ("error", f"无法连接主机:{e}"))
    if r.status_code != 200:
        try:
            msg = r.json().get("detail", r.text)
        except Exception:
            msg = r.text
        return _flash_redirect("/login", ("error", f"登录失败:{msg}"))

    body = r.json()
    with _lock:
        _session_state.update({
            "host_url": host_url, "username": username,
            "token": body["token"], "expires_at": body["expires_at"],
        })
        _config["host_url"] = host_url
        _save_config(_config)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout():
    _clear_local_session()
    return JSONResponse({"ok": True})


# ----------------------------------------------------------------------------
# /api/me /api/config /api/keys
# ----------------------------------------------------------------------------


@app.get("/api/me")
def api_me():
    if not _is_logged_in():
        return _need_login()
    return {
        "username": _session_state["username"],
        "host_url": _session_state["host_url"],
        "expires_at": _session_state["expires_at"],
    }


@app.get("/api/config")
def api_config_get():
    if not _is_logged_in():
        return _need_login()
    return _config


@app.post("/api/config")
async def api_config_set(request: Request):
    if not _is_logged_in():
        return _need_login()
    data = await request.json()
    with _lock:
        if "host_url" in data:
            _config["host_url"] = str(data["host_url"]).rstrip("/")
        if "backend" in data and data["backend"] in ("stub", "real"):
            _config["backend"] = data["backend"]
        _save_config(_config)
    return _config


@app.get("/api/keys")
def api_keys_get():
    if not _is_logged_in():
        return _need_login()
    keys = _keystore.get_paths(_session_state["username"])
    return {
        "sk_present": bool(keys and keys.sk_path.exists()),
        "sk_path": str(keys.sk_path) if keys else "",
        "evk_present": bool(keys and keys.evk_path.exists()),
        "evk_path": str(keys.evk_path) if keys else "",
        "user_auth_present": bool(keys and keys.user_auth_path and keys.user_auth_path.exists()),
        "user_auth_path": str(keys.user_auth_path) if keys and keys.user_auth_path else "",
    }


@app.post("/api/keys/sk")
async def api_keys_upload_sk(file: UploadFile = File(...)):
    if not _is_logged_in():
        return _need_login()
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    with tempfile.NamedTemporaryFile(delete=False) as t:
        t.write(data); tmp = Path(t.name)
    try:
        dst = _keystore.import_sk(username=_session_state["username"], source=tmp)
        return {"ok": True, "path": str(dst), "size_bytes": dst.stat().st_size}
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/keys/evk")
async def api_keys_upload_evk(file: UploadFile = File(...)):
    if not _is_logged_in():
        return _need_login()
    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    with tempfile.NamedTemporaryFile(delete=False) as t:
        t.write(data); tmp = Path(t.name)
    try:
        dst = _keystore.import_evk(username=_session_state["username"], source=tmp)
        return {"ok": True, "path": str(dst), "size_bytes": dst.stat().st_size}
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/keys/fetch_auth")
def api_keys_fetch_auth():
    if not _is_logged_in():
        return _need_login()
    host_url = _session_state["host_url"]
    token = _session_state["token"]
    try:
        r = httpx.get(
            f"{host_url}/auth/user_authorization",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"无法连接主机:{type(e).__name__}: {e}")
    if r.status_code == 401:
        _clear_local_session()
        raise HTTPException(502, "主机拒绝(session 已过期)· 请退出后重新登录")
    if r.status_code != 200:
        raise HTTPException(502, f"主机拒绝({r.status_code}):{r.text[:200]}")
    with tempfile.NamedTemporaryFile(delete=False) as t:
        t.write(r.content); tmp = Path(t.name)
    try:
        dst = _keystore.import_user_authorization(username=_session_state["username"], source=tmp)
        return {"ok": True, "path": str(dst), "size_bytes": dst.stat().st_size}
    finally:
        tmp.unlink(missing_ok=True)


# ----------------------------------------------------------------------------
# /api/files
# ----------------------------------------------------------------------------


@app.get("/api/files")
def api_files_list():
    if not _is_logged_in():
        return _need_login()
    out = []
    try:
        all_paths = _storage.list_ciphertexts()
    except Exception:
        all_paths = []
    for p in all_paths:
        if p.name.endswith(".meta.csv") or p.name.endswith(".schema.json"):
            continue
        size = p.stat().st_size if p.exists() else 0
        meta_p = p.with_suffix(p.suffix + ".meta.csv")
        out.append({
            "name": p.name,
            "path": str(p),
            "size_kb": round(size / 1024, 1),
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "has_meta": meta_p.exists(),
        })
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


def _smart_read(path: Path, suffix: str):
    """智能读表 + 找真实表头 + 选数据丰满的 sheet。"""
    import pandas as pd

    def _is_bad(df_) -> bool:
        cols = [str(c) for c in df_.columns]
        if not cols:
            return True
        bad = sum(1 for c in cols if c.startswith("Unnamed:") or not c.strip() or c.startswith("nan"))
        return bad >= max(1, len(cols) // 2)

    if suffix == ".csv":
        try:
            df = pd.read_csv(path)
            if _is_bad(df) or (len(df.columns) == 1 and "," in path.read_text(errors="replace")[:300]):
                import csv
                with open(path, encoding="utf-8") as f:
                    rows = list(csv.reader(f))
                # 找前 10 行最像表头的
                best, best_n = 0, -1
                for i, r in enumerate(rows[:10]):
                    s = sum(1 for v in r if v and v.strip())
                    if s >= 2 and s > best_n:
                        best, best_n = i, s
                df = pd.read_csv(path, skiprows=best)
                return df, "csv", best
            return df, "csv", 0
        except Exception:
            raise

    # xlsx
    all_sheets = pd.read_excel(path, sheet_name=None)
    best_name, best_score = None, -1
    for n, sh in all_sheets.items():
        if sh is None or sh.empty:
            continue
        score = sh.shape[0] * (sh.select_dtypes(include="number").shape[1] + 1) + sh.shape[1]
        if score > best_score:
            best_name, best_score = n, score
    if best_name is None:
        first = next(iter(all_sheets))
        return all_sheets[first], str(first), 0
    df = all_sheets[best_name]
    header_row = 0
    if _is_bad(df):
        raw = pd.read_excel(path, sheet_name=best_name, header=None)
        for i in range(min(10, len(raw))):
            r = raw.iloc[i].tolist()
            s = sum(1 for v in r if isinstance(v, str) and v.strip())
            if s >= 2:
                header_row = i
                break
        if header_row > 0:
            df = pd.read_excel(path, sheet_name=best_name, header=header_row)
    return df, str(best_name), header_row


def _ingest_plaintext_path(src_path: Path, original_name: str, *, dst_stem: Optional[str] = None) -> dict:
    """
    把一个本地明文 CSV/XLSX 文件加密入库 —— 上传端点 + 定时任务源文件夹 共用。
    返回 {name, path, encrypted_columns, plaintext_columns, row_count, ...}。
    抛 ValueError 表示解析/校验失败。
    """
    import pandas as pd

    username = _session_state["username"]
    keys = _keystore.get_paths(username)
    sk_path = keys.sk_path if keys else None
    evk_path = keys.evk_path if keys else None
    backend = _config.get("backend", "real")

    raw_suffix = src_path.suffix.lower() or ".csv"
    if raw_suffix not in (".csv", ".xlsx", ".xls"):
        raise ValueError(f"暂不支持的格式:{raw_suffix} · 仅 CSV / XLSX")

    try:
        df, sheet_name, header_row = _smart_read(src_path, raw_suffix)
    except Exception as e:
        raise ValueError(f"无法解析文件:{type(e).__name__}: {e}")
    if df.empty or df.shape[1] == 0:
        raise ValueError("文件没有任何列")

    all_cols = df.columns.tolist()
    if all(str(c).startswith("Unnamed:") for c in all_cols):
        raise ValueError("无法识别表头(所有列都是 Unnamed)")
    string_cols = df.select_dtypes(include=["object", "string"]).columns.tolist()
    numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()

    stem = dst_stem or Path(original_name).stem
    cipher_suffix = raw_suffix if backend == "real" else f"{raw_suffix}.cipher"
    dst = _storage.ciphertext_dir / (stem + "_enc" + cipher_suffix)

    nan_counts = {}
    if numeric_cols:
        num_df = df[numeric_cols].copy()
        for col in numeric_cols:
            cnt = int(num_df[col].isna().sum())
            if cnt > 0:
                nan_counts[col] = cnt
        num_df = num_df.fillna(0)
        with tempfile.NamedTemporaryFile(delete=False, suffix=raw_suffix) as t2:
            num_tmp = Path(t2.name)
        try:
            if raw_suffix == ".csv":
                num_df.to_csv(num_tmp, index=False)
            else:
                num_df.to_excel(num_tmp, index=False)
            zfhe = ZFHE(backend=backend, sk_path=sk_path, evk_path=evk_path)
            zfhe.encrypt_file(num_tmp, dst)
        finally:
            num_tmp.unlink(missing_ok=True)
    else:
        dst.write_bytes(b"")

    meta_path = ""
    if string_cols:
        meta_dst = dst.with_suffix(dst.suffix + ".meta.csv")
        df[string_cols].to_csv(meta_dst, index=False)
        meta_path = str(meta_dst)

    schema = {
        "scenario": "auto",
        "columns": [
            {"name": c, "encrypted": c in numeric_cols,
             "type": "float" if c in numeric_cols else "string"}
            for c in all_cols
        ],
        "metadata_columns": string_cols,
        "primary_key": string_cols[0] if string_cols else (numeric_cols[0] if numeric_cols else ""),
    }
    schema_dst = dst.with_suffix(dst.suffix + ".schema.json")
    schema_dst.write_text(json.dumps(schema, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "name": dst.name, "path": str(dst),
        "size_kb": round(dst.stat().st_size / 1024, 1) if dst.exists() else 0,
        "backend": backend,
        "meta_path": meta_path, "schema_path": str(schema_dst),
        "encrypted_columns": numeric_cols, "plaintext_columns": string_cols,
        "row_count": len(df),
        "sheet_name": sheet_name, "header_row": header_row,
        "column_preview": all_cols[:8],
        "nan_filled": nan_counts,
    }


@app.post("/api/files/upload")
async def api_files_upload(raw_file: UploadFile = File(...)):
    if not _is_logged_in():
        return _need_login()

    raw_bytes = await raw_file.read()
    if not raw_bytes:
        raise HTTPException(400, "数据文件为空")
    raw_suffix = Path(raw_file.filename or "data").suffix.lower() or ".csv"
    if raw_suffix not in (".csv", ".xlsx", ".xls"):
        raise HTTPException(400, f"暂不支持的格式:{raw_suffix} · 仅 CSV / XLSX")

    with tempfile.NamedTemporaryFile(delete=False, suffix=raw_suffix) as tmp:
        tmp.write(raw_bytes); tmp_path = Path(tmp.name)
    try:
        return _ingest_plaintext_path(tmp_path, raw_file.filename or "data")
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        tmp_path.unlink(missing_ok=True)


def _native_pick_folder() -> tuple[Optional[str], bool, str]:
    """
    调系统原生「选择文件夹」对话框(跨平台)。
    返回 (path 或 None, cancelled, error)。浏览器拿不到绝对路径,只能走系统对话框。
    """
    import subprocess
    import sys

    prompt = "选择数据文件夹(每次取最新文件分析)"
    plat = sys.platform

    try:
        if plat == "darwin":
            script = f'POSIX path of (choose folder with prompt "{prompt}")'
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                if "-128" in (r.stderr or "") or "User canceled" in (r.stderr or ""):
                    return None, True, ""
                return None, False, (r.stderr or "").strip()[:120]
            return (r.stdout or "").strip().rstrip("/"), False, ""

        if plat == "win32":
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$d = New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.Description = '{prompt}';"
                "$r = $d.ShowDialog();"
                "if ($r -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $d.SelectedPath }"
            )
            r = subprocess.run(
                ["powershell", "-NoProfile", "-STA", "-Command", ps],
                capture_output=True, text=True, timeout=300,
            )
            path = (r.stdout or "").strip()
            if not path:
                return None, True, ""   # 取消 = 空输出
            return path.rstrip("\\/"), False, ""

        # Linux:zenity / kdialog
        for cmd in (
            ["zenity", "--file-selection", "--directory", f"--title={prompt}"],
            ["kdialog", "--getexistingdirectory", os.path.expanduser("~")],
        ):
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            except FileNotFoundError:
                continue
            if r.returncode != 0:
                return None, True, ""   # 取消
            path = (r.stdout or "").strip()
            if path:
                return path.rstrip("/"), False, ""
            return None, True, ""
        return None, False, "未找到 zenity/kdialog · 请手动粘贴路径"

    except subprocess.TimeoutExpired:
        return None, False, "选择超时"
    except FileNotFoundError:
        return None, False, "未找到系统文件选择器 · 请手动粘贴路径"


@app.post("/api/pick_folder")
def api_pick_folder():
    """原生「选择文件夹」对话框(macOS / Windows / Linux),返回绝对路径。"""
    if not _is_logged_in():
        return _need_login()
    path, cancelled, err = _native_pick_folder()
    if cancelled:
        return {"cancelled": True}
    if err:
        raise HTTPException(500, err)
    return {"path": path, "cancelled": False}


@app.post("/api/files/text_extract")
async def api_files_text_extract(raw_file: UploadFile = File(...)):
    """明文文本附件:在内存里抽文本,不落沙盒,直接随消息发给 LLM。"""
    if not _is_logged_in():
        return _need_login()
    name = raw_file.filename or "attachment"
    if not text_extract.is_text_attachment(name):
        raise HTTPException(
            400,
            f"不支持该文本格式 · 支持:{', '.join(sorted(text_extract.SUPPORTED_EXTS))}",
        )
    data = await raw_file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    # 最大 10 MB(防止巨型 PDF/docx 把内存撑爆)
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "文本文件超过 10 MB,请拆分后再传")

    # 落到临时文件让 extractor 按路径工作
    suffix = Path(name).suffix.lower()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data); tmp_path = Path(tmp.name)
    try:
        try:
            text = text_extract.extract(tmp_path)
        except Exception as e:
            raise HTTPException(400, f"提取失败:{type(e).__name__}: {e}")
        if not text.strip():
            raise HTTPException(400, "文件解析后为空文本")
        return {
            "name": name,
            "kind": "text",
            "chars": len(text),
            "preview": text[:300],
            "content": text,
            "size_kb": round(len(data) / 1024, 1),
        }
    finally:
        tmp_path.unlink(missing_ok=True)


@app.delete("/api/files/{name}")
def api_files_delete(name: str):
    if not _is_logged_in():
        return _need_login()
    target = _storage.ciphertext_dir / name
    if not target.exists() or not target.is_relative_to(_storage.ciphertext_dir):
        raise HTTPException(404, "文件不存在或路径非法")
    target.unlink()
    target.with_suffix(target.suffix + ".meta.csv").unlink(missing_ok=True)
    target.with_suffix(target.suffix + ".schema.json").unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/files/{name}/preview")
def api_files_preview(name: str):
    if not _is_logged_in():
        return _need_login()
    target = _storage.ciphertext_dir / name
    if not target.exists() or not target.is_relative_to(_storage.ciphertext_dir):
        raise HTTPException(404, "文件不存在或路径非法")
    info: dict[str, Any] = {
        "name": target.name, "path": str(target),
        "size_kb": round(target.stat().st_size / 1024, 1),
    }
    meta_p = target.with_suffix(target.suffix + ".meta.csv")
    if meta_p.exists():
        try:
            import pandas as pd
            meta_df = pd.read_csv(meta_p)
            info["meta_columns"] = list(meta_df.columns)
            info["meta_row_count"] = len(meta_df)
            info["meta_preview"] = meta_df.head(8).fillna("").astype(str).values.tolist()
        except Exception as e:
            info["meta_error"] = str(e)
    else:
        info.update({"meta_columns": [], "meta_preview": [], "meta_row_count": 0})
    schema_p = target.with_suffix(target.suffix + ".schema.json")
    if schema_p.exists():
        try:
            info["schema"] = json.loads(schema_p.read_text(encoding="utf-8"))
        except Exception as e:
            info["schema_error"] = str(e)
    else:
        info["schema"] = None
    return info


# ----------------------------------------------------------------------------
# /api/skills —— 内置(只读) + 自定义(可增删)
# ----------------------------------------------------------------------------


@app.get("/api/skills")
def api_skills_list():
    if not _is_logged_in():
        return _need_login()
    from client import skills_loader
    return {
        "skill_md": skills_loader.list_meta(),   # SKILL.md 教学技能(代码生成主路径)
        "builtin": builtin_skills(),             # 固化 skill(兜底)
        "custom": _custom_skills.list_all(),     # 用户自定义指标
    }


@app.get("/api/skills/md/{slug}")
def api_skill_md_body(slug: str):
    """读某个 SKILL.md 的正文(给 UI「查看」用)。"""
    if not _is_logged_in():
        return _need_login()
    from client import skills_loader
    body = skills_loader.get_body(slug)
    if body is None:
        raise HTTPException(404, "skill 不存在")
    return {"slug": slug, "body": body}


@app.post("/api/skills/upload")
async def api_skills_upload(
    files: list[UploadFile] = File(...),
    paths: list[str] = Form(default=[]),
):
    """
    拖拽添加技能包(支持多文件嵌套):
      - 单个 .md      → 当 SKILL.md
      - 单个 .zip     → 解压整包
      - 多文件 + paths → 保留目录结构(SKILL.md + INDEX.md + docs/ + examples/)
    """
    if not _is_logged_in():
        return _need_login()
    from client import skills_loader

    if not files:
        raise HTTPException(400, "没有文件")

    try:
        # 单 zip
        if len(files) == 1 and (files[0].filename or "").lower().endswith(".zip"):
            data = await files[0].read()
            doc = skills_loader.add_user_skill_zip(data)
        # 单 md
        elif len(files) == 1 and (files[0].filename or "").lower().endswith(".md") and not paths:
            data = await files[0].read()
            doc = skills_loader.add_user_skill_md(
                data.decode("utf-8", errors="replace"),
                fallback_name=Path(files[0].filename or "skill").stem,
            )
        # 多文件嵌套包
        else:
            collected: list[tuple] = []
            for i, f in enumerate(files):
                data = await f.read()
                rel = paths[i] if i < len(paths) and paths[i] else (f.filename or f"file_{i}")
                collected.append((rel, data))
            doc = skills_loader.add_user_skill_files(collected)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(400, f"技能包解析失败:{type(e).__name__}: {e}")

    return doc.to_meta()


@app.delete("/api/skills/md/{slug}")
def api_skills_md_delete(slug: str):
    """删除用户拖拽添加的 SKILL.md 技能(内置不可删)。"""
    if not _is_logged_in():
        return _need_login()
    from client import skills_loader
    if not skills_loader.delete_user_skill(slug):
        raise HTTPException(404, "用户技能不存在(内置技能不可删)")
    return {"ok": True}


# ----------------------------------------------------------------------------
# /api/sessions
# ----------------------------------------------------------------------------


def _sess_for_user(sid: str) -> ChatSession:
    sess = _sessions.get(sid)
    if not sess or sess.username != _session_state["username"]:
        raise HTTPException(404, "会话不存在或无权访问")
    return sess


@app.get("/api/sessions")
def api_sessions_list():
    if not _is_logged_in():
        return _need_login()
    return [
        {
            "id": s.id, "title": s.title,
            "created_at": s.created_at, "updated_at": s.updated_at,
            "message_count": len(s.messages),
        }
        for s in _sessions.list_for(_session_state["username"])
    ]


@app.post("/api/sessions")
async def api_sessions_create():
    if not _is_logged_in():
        return _need_login()
    sess = _sessions.create(username=_session_state["username"])
    return {"id": sess.id, "title": sess.title}


@app.delete("/api/sessions/{sid}")
def api_sessions_delete(sid: str):
    if not _is_logged_in():
        return _need_login()
    _sess_for_user(sid)
    _sessions.delete(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/title")
async def api_sessions_rename(sid: str, request: Request):
    if not _is_logged_in():
        return _need_login()
    _sess_for_user(sid)
    data = await request.json()
    _sessions.rename(sid, data.get("title", ""))
    return {"ok": True}


@app.get("/api/sessions/{sid}/messages")
def api_messages_list(sid: str):
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    return {
        "session": {"id": sess.id, "title": sess.title},
        "messages": [m.to_dict() for m in sess.messages],
    }


@app.get("/api/sessions/{sid}/messages/{mid}")
def api_messages_get(sid: str, mid: str):
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    for m in sess.messages:
        if m.id == mid:
            return m.to_dict()
    raise HTTPException(404, "消息不存在")


@app.post("/api/sessions/{sid}/messages")
async def api_messages_send(sid: str, request: Request):
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    data = await request.json()
    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "消息为空")
    attached_cipher = (data.get("attached_cipher") or "").strip()
    # 明文文本附件(每条 {name, content})— 客户端已通过 /api/files/text_extract 抽好文本
    raw_text_atts = data.get("text_attachments") or []
    text_attachments: list[dict[str, str]] = []
    for t in raw_text_atts:
        if isinstance(t, dict) and t.get("content"):
            text_attachments.append({
                "name": str(t.get("name") or "attachment"),
                "content": str(t.get("content"))[:30_000],
            })

    # 0) 抓"最近历史"作为上下文 —— 最多 6 条(3 轮 user/assistant 对)
    history_for_llm: list[dict[str, str]] = []
    for m in sess.messages[-6:]:
        if m.role == "user" and m.content:
            history_for_llm.append({"role": "user", "content": m.content})
        elif m.role == "assistant" and m.status == "done" and m.summary:
            history_for_llm.append({"role": "assistant", "content": m.summary})

    # 1) 落用户消息 + assistant 占位(只持久化附件名,内容不落盘)
    user_msg = Message(
        id=secrets.token_hex(6), role="user",
        content=content, attached_cipher=attached_cipher,
        text_attachment_names=[t["name"] for t in text_attachments],
    )
    _sessions.append_message(sid, user_msg)
    asst = Message(id=secrets.token_hex(6), role="assistant", status="pending")
    _sessions.append_message(sid, asst)

    # 2) 后台跑 pipeline
    threading.Thread(
        target=_run_pipeline,
        args=(sid, asst.id, content, attached_cipher, history_for_llm, text_attachments),
        daemon=True, name=f"ask-{sid}-{asst.id}",
    ).start()
    return {"user_message": user_msg.to_dict(), "assistant_message": asst.to_dict()}


@app.post("/api/sessions/{sid}/messages/{mid}/cancel")
def api_messages_cancel(sid: str, mid: str):
    """用户点停止按钮 —— 标记该 mid 为已取消,pipeline 会在下一个检查点退出。"""
    if not _is_logged_in():
        return _need_login()
    _sess_for_user(sid)
    with _cancel_lock:
        _cancelled_msgs.add(mid)
    # 如果当前正卡在解密授权门 → 也把 event 唤醒
    with _decrypt_lock:
        _decrypt_decisions[mid] = "cancel"
        evt = _decrypt_events.get(mid)
    if evt:
        evt.set()
    return {"ok": True}


@app.post("/api/sessions/{sid}/messages/{mid}/decrypt_decision")
async def api_decrypt_decision(sid: str, mid: str, request: Request):
    """解密授权门:用户在浮卡上选了 decrypt / keep_encrypted。"""
    if not _is_logged_in():
        return _need_login()
    _sess_for_user(sid)
    data = await request.json()
    choice = (data.get("choice") or "").strip()
    if choice not in ("decrypt", "keep_encrypted", "cancel"):
        raise HTTPException(400, "choice 必须是 decrypt / keep_encrypted / cancel")
    with _decrypt_lock:
        _decrypt_decisions[mid] = choice
        evt = _decrypt_events.get(mid)
    if evt:
        evt.set()
    return {"ok": True, "choice": choice}


def _run_pipeline(
    sid: str, asst_mid: str, user_query: str, attached_cipher: str,
    history: list[dict[str, str]],
    text_attachments: list[dict[str, str]],
    output_mode: str = "interactive",
    sched_task: Optional[Any] = None,   # 定时密态任务对象(用于回填 EncryptedResult)
) -> None:
    t0 = time.time()
    _sessions.update_message(sid, asst_mid, status="running")
    steps: list[dict[str, Any]] = []

    def on_step(kind: str, label: str):
        steps.append({"kind": kind, "label": label})
        _sessions.update_message(sid, asst_mid, steps=list(steps))

    # 决定用哪份 cipher:这条消息带的 > 上一条 user 消息的
    sess = _sessions.get(sid)
    cipher_path = None
    used_cipher = ""
    if attached_cipher and Path(attached_cipher).exists():
        cipher_path = Path(attached_cipher)
        used_cipher = attached_cipher
    elif sess:
        last = sess.last_attached_cipher()
        if last and Path(last).exists():
            cipher_path = Path(last)
            used_cipher = last

    try:
        base_prompt = load_system_prompt()
        # 用户自定义指标 / 公式(企业口径)—— 固化路径追加到 prompt;codegen 路径单独传
        custom_block = build_custom_skills_prompt_block(_custom_skills.list_all())
        system_prompt = base_prompt + ("\n" + custom_block if custom_block else "")
    except Exception as e:
        _sessions.update_message(
            sid, asst_mid, status="failed",
            error=f"系统 prompt 加载失败:{e}",
            duration_sec=round(time.time() - t0, 2),
        )
        return

    def _should_cancel() -> bool:
        with _cancel_lock:
            return asst_mid in _cancelled_msgs

    def _prompt_decrypt() -> str:
        """解密授权门 · 阻塞等用户在浮卡上点选择(最长 5 分钟)。"""
        evt = threading.Event()
        with _decrypt_lock:
            _decrypt_events[asst_mid] = evt
            _decrypt_decisions.pop(asst_mid, None)
        _sessions.update_message(sid, asst_mid, status="awaiting_decrypt")
        waited = 0.0
        try:
            while waited < 300.0:
                if evt.wait(timeout=0.5):
                    break
                if _should_cancel():
                    return "cancel"
                waited += 0.5
            with _decrypt_lock:
                choice = _decrypt_decisions.pop(asst_mid, "cancel")
            # 拿到选择后把状态切回 running,前端轮询能看到 trace 继续
            _sessions.update_message(sid, asst_mid, status="running")
            return choice
        finally:
            with _decrypt_lock:
                _decrypt_events.pop(asst_mid, None)

    run_id = secrets.token_hex(6)
    try:
        result = pipeline_mod.ask(
            user_query=user_query,
            cipher_path=cipher_path,
            host_url=_session_state["host_url"],
            token=_session_state["token"],
            system_prompt=system_prompt,
            on_step=on_step,
            should_cancel=_should_cancel,
            history=history,
            text_attachments=text_attachments,
            prompt_decrypt=_prompt_decrypt,
            custom_block=custom_block,
            output_mode=output_mode,
            run_id=run_id,
        )
    except Exception as e:
        _sessions.update_message(
            sid, asst_mid, status="failed",
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1000:]}",
            duration_sec=round(time.time() - t0, 2),
            used_cipher=used_cipher,
        )
        return
    finally:
        # 不管成功失败都把 cancel 标记清掉,避免泄漏
        with _cancel_lock:
            _cancelled_msgs.discard(asst_mid)

    status = result.get("status", "failed")
    if status == "needs_cipher":
        _sessions.update_message(
            sid, asst_mid, status="needs_cipher",
            summary=result.get("summary", ""),
            duration_sec=round(time.time() - t0, 2),
        )
        return
    if status == "cancelled":
        _sessions.update_message(
            sid, asst_mid, status="cancelled",
            summary=result.get("summary", "") or "已停止 · 用户取消",
            error="",
            skill_calls=result.get("skill_calls", []),
            used_cipher=used_cipher,
            duration_sec=round(time.time() - t0, 2),
        )
        return
    if status == "failed":
        err = result.get("error", "未知错误")
        # 401 自动清 session
        if "401" in err or "登录已过期" in err:
            _clear_local_session()
        _sessions.update_message(
            sid, asst_mid, status="failed", error=err,
            summary=result.get("summary", ""),
            duration_sec=round(time.time() - t0, 2),
            used_cipher=used_cipher,
        )
        return

    # 定时密态:结果加密暂存 → 累积成 EncryptedResult(按任务聚合,待批量解密)
    if status == "encrypted_pending":
        enc = result.get("encrypted_run") or {}
        if sched_task is not None and enc.get("manifest"):
            _enc_results.add(
                username=sched_task.username, task_id=sched_task.id,
                task_name=sched_task.name, run_id=enc.get("run_id", run_id),
                run_at=datetime.now().isoformat(timespec="seconds"),
                question=sched_task.question, manifest=enc.get("manifest", []),
            )
        _sessions.update_message(
            sid, asst_mid, status="done",
            summary=result.get("summary", ""),
            skill_calls=result.get("skill_calls", []),
            used_cipher=used_cipher,
            duration_sec=round(time.time() - t0, 2),
        )
        return

    excel_path = result.get("excel_path", "")
    _sessions.update_message(
        sid, asst_mid, status="done",
        summary=result.get("summary", ""),
        excel_path=excel_path,
        excel_name=Path(excel_path).name if excel_path else "",
        skill_calls=result.get("skill_calls", []),
        used_cipher=used_cipher,
        duration_sec=round(time.time() - t0, 2),
    )


# ----------------------------------------------------------------------------
# 定时任务(MVP)
# ----------------------------------------------------------------------------


def _ensure_task_session(task) -> str:
    """确保任务有一个聊天会话(累积它的历次运行);返回 session_id。"""
    sid = task.session_id
    sess = _sessions.get(sid) if sid else None
    if not sess:
        sess = _sessions.create(username=task.username, title=f"⏰ {task.name}")
        _task_store.update(task.id, session_id=sess.id)
        sid = sess.id
    return sid


def _launch_run(*, username: str, task_name: str, question: str,
                cipher_path: str, session_id: str,
                output_mode: str = "interactive", sched_task=None) -> None:
    """把一次运行注入聊天会话并跑 pipeline。output_mode=encrypted_sandbox 时密态结果加密暂存。"""
    sess = _sessions.get(session_id)
    if not sess:
        return
    history_for_llm: list[dict[str, str]] = []
    for m in sess.messages[-6:]:
        if m.role == "user" and m.content:
            history_for_llm.append({"role": "user", "content": m.content})
        elif m.role == "assistant" and m.status == "done" and m.summary:
            history_for_llm.append({"role": "assistant", "content": m.summary})

    user_msg = Message(id=secrets.token_hex(6), role="user",
                       content=question, attached_cipher=cipher_path or "")
    _sessions.append_message(session_id, user_msg)
    asst = Message(id=secrets.token_hex(6), role="assistant", status="pending")
    _sessions.append_message(session_id, asst)
    threading.Thread(
        target=_run_pipeline,
        args=(session_id, asst.id, question, cipher_path or "", history_for_llm, []),
        kwargs={"output_mode": output_mode, "sched_task": sched_task},
        daemon=True, name=f"sched-{session_id}-{asst.id}",
    ).start()


# 源文件夹加密入库缓存:同一文件(路径+mtime)只加密一次,供同文件夹的多个任务复用
_folder_ingest_cache: dict[tuple, str] = {}
_folder_ingest_lock = threading.Lock()


def _pick_latest_in_folder(folder: str, pattern: str = "") -> Optional[Path]:
    """挑文件夹里最新的 CSV/XLSX(按修改时间)。"""
    d = Path(folder).expanduser()
    if not d.is_dir():
        return None
    pats = [pattern] if pattern else ["*.csv", "*.xlsx", "*.xls"]
    files: list[Path] = []
    for p in pats:
        files.extend(d.glob(p))
    files = [f for f in files if f.is_file() and not f.name.startswith("~$")]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def _resolve_task_cipher(task) -> tuple[Optional[str], str]:
    """
    解析任务这次运行该用哪份密文。
    返回 (cipher_path 或 None, 说明/错误)。
    - 绑源文件夹:取最新明文 → 加密入库(带 mtime 缓存)→ 返回密文路径
    - 绑固定密文:直接返回
    """
    if task.source_folder:
        latest = _pick_latest_in_folder(task.source_folder, task.source_pattern or "")
        if latest is None:
            return None, f"源文件夹无可处理文件:{task.source_folder}"
        key = (str(latest.resolve()), latest.stat().st_mtime)
        with _folder_ingest_lock:
            cached = _folder_ingest_cache.get(key)
        if cached and Path(cached).exists():
            return cached, f"复用已加密的最新文件:{latest.name}"
        try:
            info = _ingest_plaintext_path(latest, latest.name)
        except Exception as e:
            return None, f"最新文件加密入库失败:{e}"
        with _folder_ingest_lock:
            _folder_ingest_cache[key] = info["path"]
        return info["path"], f"已加密最新文件:{latest.name}"
    if task.cipher_path:
        return task.cipher_path, ""
    return None, ""


def _on_scheduler_fire(task) -> None:
    """调度器到点回调(在 scheduler 线程里)。"""
    # 密态分析(有数据:固定密文 / 源文件夹)→ 正常计算,结果加密暂存,累积待批量解密
    if task.needs_approval:
        if not (_is_logged_in() and _session_state.get("username") == task.username):
            # 仅活跃会话:没登录就不跑(下次到点再说)
            _run_history.add(
                username=task.username, task_id=task.id, task_name=task.name,
                ran_at=datetime.now().isoformat(timespec="seconds"),
                status="skipped", summary="到点时未登录 · 跳过(仅活跃会话)",
            )
            return
        cipher_path, note = _resolve_task_cipher(task)
        if not cipher_path:
            _run_history.add(
                username=task.username, task_id=task.id, task_name=task.name,
                ran_at=datetime.now().isoformat(timespec="seconds"),
                status="skipped", summary=note or "无可用数据 · 跳过",
            )
            return
        sid = _ensure_task_session(task)
        _launch_run(username=task.username, task_name=task.name,
                    question=task.question, cipher_path=cipher_path,
                    session_id=sid, output_mode="encrypted_sandbox", sched_task=task)
        _run_history.add(
            username=task.username, task_id=task.id, task_name=task.name,
            ran_at=datetime.now().isoformat(timespec="seconds"),
            status="launched",
            summary=f"密态计算 · 结果加密暂存 · 待批量解密{(' · ' + note) if note else ''}",
        )
        return
    # 自由问答(无密文)→ 仅在当前已登录且是该用户时直接跑;否则也入队
    if _is_logged_in() and _session_state.get("username") == task.username:
        sid = _ensure_task_session(task)
        _launch_run(username=task.username, task_name=task.name,
                    question=task.question, cipher_path="", session_id=sid)
        _run_history.add(
            username=task.username, task_id=task.id, task_name=task.name,
            ran_at=datetime.now().isoformat(timespec="seconds"),
            status="launched", summary="已自动运行 · 见会话",
        )
    else:
        sid = _ensure_task_session(task)
        _pending_store.add(
            username=task.username, task_id=task.id, task_name=task.name,
            question=task.question, cipher_path="",
            session_id=sid, due_at=task.next_run or "",
        )
        _run_history.add(
            username=task.username, task_id=task.id, task_name=task.name,
            ran_at=datetime.now().isoformat(timespec="seconds"),
            status="queued", summary="到点时未登录 · 已入待批队列",
        )


_scheduler = Scheduler(_task_store, _on_scheduler_fire, poll_seconds=30)


@app.on_event("startup")
def _arm_scheduler():
    _scheduler.start()


@app.get("/api/scheduled_tasks")
def api_tasks_list():
    if not _is_logged_in():
        return _need_login()
    u = _session_state["username"]
    return {
        "tasks": [t.to_dict() for t in _task_store.list_for(u)],
        "pending_count": _pending_store.count_pending(u) + _enc_results.count_pending(u),
    }


@app.post("/api/scheduled_tasks/parse_schedule")
async def api_parse_schedule(request: Request):
    """自然语言排程 → cron(普通用户用大白话,实时转换)。"""
    if not _is_logged_in():
        return _need_login()
    from client.webui.scheduler import parse_natural_schedule
    data = await request.json()
    return parse_natural_schedule(data.get("text", ""))


@app.post("/api/scheduled_tasks")
async def api_tasks_create(request: Request):
    if not _is_logged_in():
        return _need_login()
    data = await request.json()
    name = (data.get("name") or "").strip()
    question = (data.get("question") or "").strip()
    if not name or not question:
        raise HTTPException(400, "任务名和问题都不能为空")
    kind = data.get("schedule_kind", "daily")
    if kind not in ("interval", "daily", "weekly", "monthly", "cron"):
        raise HTTPException(400, "schedule_kind 只能是 interval/daily/weekly/monthly/cron")
    cron_expr = (data.get("cron_expr") or "").strip()
    if kind == "cron":
        if not cron_expr:
            raise HTTPException(400, "cron 模式必须填 cron 表达式")
        try:
            from croniter import croniter
            if not croniter.is_valid(cron_expr):
                raise ValueError("非法 cron 表达式")
        except ImportError:
            raise HTTPException(500, "未安装 croniter")
        except Exception:
            raise HTTPException(400, "cron 表达式不合法(标准 5 段:分 时 日 月 周)")
    cipher_path = (data.get("cipher_path") or "").strip()
    source_folder = (data.get("source_folder") or "").strip()
    source_pattern = (data.get("source_pattern") or "").strip()
    if cipher_path and not Path(cipher_path).exists():
        raise HTTPException(400, "指定的密文文件不存在")
    if source_folder:
        d = Path(source_folder).expanduser()
        if not d.is_dir():
            raise HTTPException(400, f"源文件夹不存在:{source_folder}")
        source_folder = str(d)
    t = _task_store.create(
        username=_session_state["username"], name=name, question=question,
        cipher_path=cipher_path, source_folder=source_folder, source_pattern=source_pattern,
        schedule_kind=kind, cron_expr=cron_expr,
        cron_readable=(data.get("cron_readable") or "").strip(),
        interval_minutes=int(data.get("interval_minutes", 60) or 60),
        at_hour=int(data.get("at_hour", 9) or 0),
        at_minute=int(data.get("at_minute", 0) or 0),
        weekday=int(data.get("weekday", 0) or 0),
        day_of_month=int(data.get("day_of_month", 1) or 1),
        enabled=bool(data.get("enabled", True)),
    )
    return t.to_dict()


@app.patch("/api/scheduled_tasks/{tid}")
async def api_tasks_patch(tid: str, request: Request):
    if not _is_logged_in():
        return _need_login()
    t = _task_store.get(tid)
    if not t or t.username != _session_state["username"]:
        raise HTTPException(404, "任务不存在")
    data = await request.json()
    patch = {k: data[k] for k in ("name", "question", "enabled", "schedule_kind",
                                  "interval_minutes", "at_hour", "at_minute", "weekday",
                                  "day_of_month", "cron_expr", "cron_readable",
                                  "cipher_path", "source_folder", "source_pattern") if k in data}
    t = _task_store.update(tid, **patch)
    return t.to_dict()


@app.delete("/api/scheduled_tasks/{tid}")
def api_tasks_delete(tid: str):
    if not _is_logged_in():
        return _need_login()
    t = _task_store.get(tid)
    if not t or t.username != _session_state["username"]:
        raise HTTPException(404, "任务不存在")
    _task_store.delete(tid)
    return {"ok": True}


@app.post("/api/scheduled_tasks/{tid}/run_now")
def api_tasks_run_now(tid: str):
    """手动立即跑一次(等同到点触发)。"""
    if not _is_logged_in():
        return _need_login()
    t = _task_store.get(tid)
    if not t or t.username != _session_state["username"]:
        raise HTTPException(404, "任务不存在")
    _on_scheduler_fire(t)
    # fire 后 task 已被 _ensure_task_session 绑上 session_id;自由问答任务已在该会话开跑
    t2 = _task_store.get(tid)
    return {
        "ok": True,
        "needs_approval": t.needs_approval,
        "session_id": (t2.session_id if t2 else "") or "",
    }


@app.get("/api/scheduled_tasks/pending")
def api_pending_list():
    if not _is_logged_in():
        return _need_login()
    u = _session_state["username"]
    # 两类待批:① 自由问答的待跑(PendingRun)② 密态任务的加密结果(按任务聚合)
    runs = [dict(p.to_dict(), kind="run") for p in _pending_store.list_pending(u)]
    encrypted = [dict(a, kind="decrypt") for a in _enc_results.aggregate_by_task(u)]
    return {"runs": runs, "encrypted": encrypted}


@app.post("/api/scheduled_tasks/decrypt/{task_id}")
def api_task_decrypt(task_id: str):
    """批量解密一个密态任务累积的所有加密结果 → 落到一个文件夹。"""
    if not _is_logged_in():
        return _need_login()
    u = _session_state["username"]
    items = _enc_results.pending_for_task(task_id)
    items = [r for r in items if r.username == u]
    if not items:
        raise HTTPException(404, "该任务没有待解密的结果")
    task = _task_store.get(task_id)
    folder_name = (task.name if task else items[0].task_name) or "定时任务结果"
    runs = [{"run_id": r.run_id, "run_at": r.run_at, "manifest": r.manifest,
             "question": r.question} for r in items]
    try:
        from client.webui import sched_results
        out_dir = sched_results.decrypt_runs_to_folder(runs, folder_name)
    except Exception as e:
        raise HTTPException(500, f"批量解密失败:{type(e).__name__}: {e}")
    # 标记已解密 + 清沙盒密文
    _enc_results.mark_decrypted([r.id for r in items])
    try:
        from client.webui import sched_results as _sr
        _sr.cleanup_runs([r.run_id for r in items])
    except Exception:
        pass
    _run_history.add(
        username=u, task_id=task_id, task_name=folder_name,
        ran_at=datetime.now().isoformat(timespec="seconds"),
        status="decrypted", summary=f"已批量解密 {len(items)} 次运行 → 文件夹 {out_dir.name}",
    )
    return {"ok": True, "folder": str(out_dir), "count": len(items)}


@app.post("/api/scheduled_tasks/pending/{pid}/approve")
def api_pending_approve(pid: str):
    """批准一个待批运行 → 注入会话并跑(此时人在场,走正常解密授权卡)。"""
    if not _is_logged_in():
        return _need_login()
    p = _pending_store.get(pid)
    if not p or p.username != _session_state["username"] or p.status != "pending":
        raise HTTPException(404, "待批运行不存在")
    sid = p.session_id or ""
    if not _sessions.get(sid):
        sess = _sessions.create(username=p.username, title=f"⏰ {p.task_name}")
        sid = sess.id
    _launch_run(username=p.username, task_name=p.task_name,
                question=p.question, cipher_path=p.cipher_path, session_id=sid)
    _pending_store.set_status(pid, "approved")
    _run_history.add(
        username=p.username, task_id=p.task_id, task_name=p.task_name,
        ran_at=datetime.now().isoformat(timespec="seconds"),
        status="launched", summary="已批准运行 · 见会话",
    )
    return {"ok": True, "session_id": sid}


@app.post("/api/scheduled_tasks/pending/{pid}/dismiss")
def api_pending_dismiss(pid: str):
    if not _is_logged_in():
        return _need_login()
    p = _pending_store.get(pid)
    if not p or p.username != _session_state["username"]:
        raise HTTPException(404, "待批运行不存在")
    _pending_store.set_status(pid, "dismissed")
    return {"ok": True}


@app.get("/api/scheduled_tasks/history")
def api_tasks_history():
    if not _is_logged_in():
        return _need_login()
    return [r.to_dict() for r in _run_history.list_for(_session_state["username"])]


# ----------------------------------------------------------------------------
# Excel 下载(B6-2 白名单)
# ----------------------------------------------------------------------------


@app.get("/api/excel/download")
def api_excel_download(path: str):
    if not _is_logged_in():
        return _need_login()
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Excel 文件不存在")
    allowed_roots = [Path.home() / "Downloads", APP_DATA_DIR]
    if not any(p.resolve().is_relative_to(r.resolve()) for r in allowed_roots if r.exists()):
        raise HTTPException(403, "拒绝下载白名单外的文件")
    return FileResponse(
        p,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=p.name,
    )
