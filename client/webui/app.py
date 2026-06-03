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

# 解密授权门(B6-1):
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


@app.post("/api/files/upload")
async def api_files_upload(raw_file: UploadFile = File(...)):
    if not _is_logged_in():
        return _need_login()
    username = _session_state["username"]
    keys = _keystore.get_paths(username)
    sk_path = keys.sk_path if keys else None
    evk_path = keys.evk_path if keys else None
    backend = _config.get("backend", "real")

    raw_bytes = await raw_file.read()
    if not raw_bytes:
        raise HTTPException(400, "数据文件为空")
    raw_suffix = Path(raw_file.filename or "data").suffix.lower() or ".csv"
    if raw_suffix not in (".csv", ".xlsx", ".xls"):
        raise HTTPException(400, f"暂不支持的格式:{raw_suffix} · 仅 CSV / XLSX")

    with tempfile.NamedTemporaryFile(delete=False, suffix=raw_suffix) as tmp:
        tmp.write(raw_bytes); tmp_path = Path(tmp.name)

    try:
        import pandas as pd
        try:
            df, sheet_name, header_row = _smart_read(tmp_path, raw_suffix)
        except Exception as e:
            raise HTTPException(400, f"无法解析文件:{type(e).__name__}: {e}")
        if df.empty or df.shape[1] == 0:
            raise HTTPException(400, "文件没有任何列")

        all_cols = df.columns.tolist()
        if all(str(c).startswith("Unnamed:") for c in all_cols):
            raise HTTPException(400, "无法识别表头(所有列都是 Unnamed)")
        string_cols = df.select_dtypes(include=["object", "string"]).columns.tolist()
        numeric_cols = df.select_dtypes(include=["number"]).columns.tolist()

        stem = Path(raw_file.filename or "data").stem
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

        # 自动 schema
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
    finally:
        tmp_path.unlink(missing_ok=True)


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
    """B6-1 授权门:用户在浮卡上选了 decrypt / keep_encrypted。"""
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
        system_prompt = load_system_prompt()
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
        """B6-1 授权门 · 阻塞等用户在浮卡上点选择(最长 5 分钟)。"""
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
