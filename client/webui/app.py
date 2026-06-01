"""
客户端 Web UI(ChatGPT 风格,单页应用)。

启动:uvicorn client.webui:app --host 127.0.0.1 --port 8444

设计:
- 单用户桌面应用 → 进程内 session(假设本机访问)
- 配置(host_url / backend)持久化到 ~/.agent-system/client-config.json
- 多会话(ChatSession)持久化到 ~/.agent-system/sessions/{id}.json
- 每次提问 → 后台线程跑 workflow → 前端 polling assistant message status

主路由:
- GET  /              单页 chat UI(未登录跳 /login)
- GET  /login         登录页
- POST /login         登录提交
- POST /logout

JSON API(给前端 JS 调):
- GET    /api/me                     当前会话信息(用户名 / token 过期)
- GET    /api/config                 本地配置(host_url / backend / auto_approve)
- POST   /api/config                 保存本地配置
- GET    /api/keys                   密钥状态
- POST   /api/keys                   上传 sk / evk / user_auth(multipart)

- GET    /api/files                  本地密文文件列表
- POST   /api/files/upload           上传原始数据 → 加密入库(可附 meta)
- DELETE /api/files/{name}           删除密文 + meta sidecar

- GET    /api/sessions               会话列表
- POST   /api/sessions               新建空会话
- DELETE /api/sessions/{sid}         删除会话
- POST   /api/sessions/{sid}/title   重命名会话
- POST   /api/sessions/{sid}/context 设置会话上下文(ciphertext / schema)

- GET    /api/sessions/{sid}/messages          所有消息
- POST   /api/sessions/{sid}/messages          发送一条用户消息 → 起后台 job
- GET    /api/sessions/{sid}/messages/{mid}    单条消息(给前端 polling 用)

- GET    /api/excel/{filename}/download       下载某次任务产出的 Excel
"""

from __future__ import annotations

import json
import os
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
from client.llm_client import HTTPLLMClient
from client.local_storage import LocalStorage
from client.permissions import AutoApproveAuthorizer
from client.skill_workflow import build_workflow
from client.tools import HELearn, HENumpy, HETorch, PandaSeal, ZFHE
from client.webui.sessions import ChatSession, Message, SessionStore

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------

_WEBUI_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEBUI_DIR / "templates"))

APP_DATA_DIR = Path.home() / ".agent-system"
CLIENT_CONFIG_FILE = APP_DATA_DIR / "client-config.json"


# ---------------------------------------------------------------------------
# 进程内状态(单用户桌面应用)
# ---------------------------------------------------------------------------

_lock = threading.Lock()

_session_state: dict[str, Any] = {
    "host_url": "",
    "username": "",
    "token": "",
    "expires_at": "",
}


def _load_config() -> dict[str, Any]:
    defaults = {
        "host_url": "http://127.0.0.1:8443",
        "backend": "stub",
        "auto_approve": True,    # Web UI 模式默认自动通过(没有 stdin 交互)
    }
    if CLIENT_CONFIG_FILE.exists():
        try:
            data = json.loads(CLIENT_CONFIG_FILE.read_text(encoding="utf-8"))
            return {**defaults, **data}
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


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="agent-system client", version="0.2.0")
app.mount("/static", StaticFiles(directory=str(_WEBUI_DIR / "static")), name="static")


def _is_logged_in() -> bool:
    return bool(_session_state.get("token") and _session_state.get("username"))


def _need_login() -> JSONResponse:
    return JSONResponse({"error": "not_logged_in"}, status_code=401)


def _flash_redirect(url: str, *messages: tuple[str, str]) -> RedirectResponse:
    if messages:
        parts = [f"_flash={quote(f'{c}|{m}', safe='')}" for c, m in messages]
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{'&'.join(parts)}"
    return RedirectResponse(url, status_code=303)


def _pop_messages(request: Request) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for val in parse_qs(request.url.query).get("_flash", []):
        if "|" in val:
            cat, msg = val.split("|", 1)
            out.append((cat, msg))
    return out


# ---------------------------------------------------------------------------
# 登录 / 登出 / 主页
# ---------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _is_logged_in():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "index.html",
        {"username": _session_state["username"]},
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
            "host_url": host_url,
            "username": username,
            "token": body["token"],
            "expires_at": body["expires_at"],
        })
        _config["host_url"] = host_url
        _save_config(_config)
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
def logout():
    with _lock:
        _session_state.update({"host_url": "", "username": "", "token": "", "expires_at": ""})
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# JSON API:用户 / 配置 / 密钥
# ---------------------------------------------------------------------------


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
        if "auto_approve" in data:
            _config["auto_approve"] = bool(data["auto_approve"])
        _save_config(_config)
    return _config


@app.get("/api/keys")
def api_keys_get():
    if not _is_logged_in():
        return _need_login()
    username = _session_state["username"]
    keys = _keystore.get_paths(username)
    # 即便没全套密钥,也能返回沙盒目录(给 UI 提示)
    vault = _keystore.vault_path(username)
    audit = _keystore.sandbox_audit(username)
    sk_p = vault / "sk.bin"
    evk_p = vault / "evk.bin"
    auth_p = vault / "user_authorization"
    return {
        "sk_present": sk_p.exists(),
        "sk_path": str(sk_p),
        "sk_size": sk_p.stat().st_size if sk_p.exists() else 0,
        "evk_present": evk_p.exists(),
        "evk_path": str(evk_p),
        "evk_size": evk_p.stat().st_size if evk_p.exists() else 0,
        "user_auth_present": auth_p.exists(),
        "user_auth_path": str(auth_p),
        "user_auth_size": auth_p.stat().st_size if auth_p.exists() else 0,
        "vault_path": str(vault),
        "sandbox": audit,
    }


@app.post("/api/keys/sk")
async def api_keys_upload_sk(file: UploadFile = File(...)):
    """单独上传 sk(解密密钥)→ 沙盒。"""
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
    """单独上传 evk(计算密钥)→ 沙盒。"""
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
    """
    从主机 admin 端拉取当前用户绑定的 user_authorization 文件,
    存到本地沙盒。这是证书的唯一来源(用户不能上传)。

    错误处理:
    - 客户端自己没登录 → 401(JS 会重定向到 /login,正常)
    - 主机端任何非 200(包括 401 / 404 / 502)→ 统一返回 502 + 中文 message
      这样浏览器看到的是"下游错误",不会被 JS 误认为"客户端 session 过期"
      而触发自动 /login 跳转(否则点一次按钮设置页就被踢出)
    """
    if not _is_logged_in():
        return _need_login()
    username = _session_state["username"]
    host_url = _session_state["host_url"]
    token = _session_state["token"]

    try:
        r = httpx.get(
            f"{host_url}/auth/user_authorization",
            headers={"Authorization": f"Bearer {token}"},
            timeout=30,
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"无法连接主机({host_url}):{type(e).__name__}: {e}")

    if r.status_code != 200:
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = r.text[:200]
        # 主机告诉我们 session 失效 → 顺手清掉本地 session,告知用户重新登录
        if r.status_code == 401:
            with _lock:
                _session_state.update({"host_url": "", "username": "", "token": "", "expires_at": ""})
            raise HTTPException(
                502,
                f"主机拒绝(401):{detail or 'session 已过期'}。"
                f"请退出登录后用新密码 / 新 token 重新登录。",
            )
        if r.status_code == 404:
            raise HTTPException(
                502,
                f"主机找不到此用户的授权(404):{detail or 'admin 端可能已删除或吊销证书'}。"
                f"请联系管理员重新颁发。",
            )
        # 其他 4xx/5xx 一律包装成 502,绝不原样把 401/403 透到浏览器,以免 JS 误判
        raise HTTPException(502, f"主机拒绝({r.status_code}):{detail or r.reason_phrase}")

    if not r.content:
        raise HTTPException(502, "主机返回空文件")

    with tempfile.NamedTemporaryFile(delete=False) as t:
        t.write(r.content); tmp = Path(t.name)
    try:
        dst = _keystore.import_user_authorization(username=username, source=tmp)
        return {
            "ok": True,
            "path": str(dst),
            "size_bytes": dst.stat().st_size,
            "source": "host",
        }
    finally:
        tmp.unlink(missing_ok=True)


@app.delete("/api/keys/{name}")
def api_keys_delete(name: str):
    """删除某把密钥(sk / evk / user_authorization),重新从源处拉。"""
    if not _is_logged_in():
        return _need_login()
    if name not in ("sk", "evk", "user_authorization"):
        raise HTTPException(400, f"未知密钥名:{name}")
    vault = _keystore.vault_path(_session_state["username"])
    target = vault / ("sk.bin" if name == "sk" else "evk.bin" if name == "evk" else "user_authorization")
    if target.exists():
        target.unlink()
    return {"ok": True}


# ---------------------------------------------------------------------------
# JSON API:密文文件
# ---------------------------------------------------------------------------


@app.get("/api/files")
def api_files_list():
    if not _is_logged_in():
        return _need_login()
    out = []
    try:
        all_paths = _storage.list_ciphertexts()
    except Exception:
        all_paths = []
    meta_names = {p.name for p in all_paths if p.name.endswith(".meta.csv")}
    for p in all_paths:
        if p.name.endswith(".meta.csv"):
            continue
        size = p.stat().st_size if p.exists() else 0
        out.append({
            "name": p.name,
            "path": str(p),
            "size_kb": round(size / 1024, 1),
            "mtime": datetime.fromtimestamp(p.stat().st_mtime).isoformat(timespec="seconds"),
            "has_meta": (p.name + ".meta.csv") in meta_names,
        })
    out.sort(key=lambda f: f["mtime"], reverse=True)
    return out


@app.post("/api/files/upload")
async def api_files_upload(
    raw_file: UploadFile = File(...),
    meta_file: Optional[UploadFile] = File(None),
):
    if not _is_logged_in():
        return _need_login()
    username = _session_state["username"]
    keys = _keystore.get_paths(username)
    sk_path = keys.sk_path if keys else None
    evk_path = keys.evk_path if keys else None
    backend = _config.get("backend", "stub")

    raw_bytes = await raw_file.read()
    if not raw_bytes:
        raise HTTPException(400, "数据文件为空")
    raw_suffix = Path(raw_file.filename or "data").suffix or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=raw_suffix) as tmp:
        tmp.write(raw_bytes); tmp_path = Path(tmp.name)

    try:
        zfhe = ZFHE(backend=backend, sk_path=sk_path, evk_path=evk_path)
        cipher_suffix = raw_suffix if backend == "real" else f"{raw_suffix}.cipher"
        stem = Path(raw_file.filename or "data").stem
        dst = _storage.ciphertext_dir / (stem + "_enc" + cipher_suffix)
        zfhe.encrypt_file(tmp_path, dst)

        meta_path = ""
        if meta_file is not None and meta_file.filename:
            meta_bytes = await meta_file.read()
            if meta_bytes:
                meta_suffix = Path(meta_file.filename).suffix.lower()
                meta_dst = dst.with_suffix(dst.suffix + ".meta.csv")
                if meta_suffix == ".csv":
                    meta_dst.write_bytes(meta_bytes)
                elif meta_suffix in (".xlsx", ".xls"):
                    import pandas as pd
                    with tempfile.NamedTemporaryFile(delete=False, suffix=meta_suffix) as t:
                        t.write(meta_bytes); meta_tmp = Path(t.name)
                    try:
                        pd.read_excel(meta_tmp).to_csv(meta_dst, index=False)
                    finally:
                        meta_tmp.unlink(missing_ok=True)
                meta_path = str(meta_dst)
        return {
            "name": dst.name, "path": str(dst),
            "size_kb": round(dst.stat().st_size / 1024, 1),
            "backend": backend, "meta_path": meta_path,
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
    return {"ok": True}


# ---------------------------------------------------------------------------
# JSON API:会话 + 消息
# ---------------------------------------------------------------------------


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
            "context_ciphertext": Path(s.context_ciphertext).name if s.context_ciphertext else "",
            "has_schema": bool(s.context_schema),
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


@app.post("/api/sessions/{sid}/context")
async def api_sessions_set_context(sid: str, request: Request):
    if not _is_logged_in():
        return _need_login()
    _sess_for_user(sid)
    data = await request.json()
    _sessions.set_context(
        sid,
        ciphertext=data.get("ciphertext"),
        schema=data.get("schema"),
    )
    return {"ok": True}


@app.get("/api/sessions/{sid}/messages")
def api_messages_list(sid: str):
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    return {
        "session": {
            "id": sess.id, "title": sess.title,
            "context_ciphertext": sess.context_ciphertext,
            "context_ciphertext_name": Path(sess.context_ciphertext).name if sess.context_ciphertext else "",
            "context_schema": sess.context_schema,
        },
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
    """
    用户发一条消息 → 立即落库 → 起后台 job 跑 workflow → 返回 user + assistant 两条
    前端拿 assistant.id 后 polling 单条消息直到 status=done/failed
    """
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    data = await request.json()
    content = (data.get("content") or "").strip()
    if not content:
        raise HTTPException(400, "消息为空")
    attachments = data.get("attachments") or []
    # 可选:更新会话上下文(前端可能换了密文 / schema)
    ctx = data.get("context") or {}
    if "ciphertext" in ctx:
        _sessions.set_context(sid, ciphertext=ctx.get("ciphertext"))
    if "schema" in ctx:
        _sessions.set_context(sid, schema=ctx.get("schema"))

    # 1) 用户消息
    user_msg = Message(
        id=secrets.token_hex(6),
        role="user",
        content=content,
        attachments=attachments,
    )
    _sessions.append_message(sid, user_msg)

    # 2) Assistant 占位(pending)
    asst_msg = Message(
        id=secrets.token_hex(6),
        role="assistant",
        status="pending",
    )
    _sessions.append_message(sid, asst_msg)

    # 3) 起后台线程
    threading.Thread(
        target=_run_workflow_for_message,
        args=(sid, asst_msg.id, content),
        daemon=True,
        name=f"chat-{sid}-{asst_msg.id}",
    ).start()

    return {
        "user_message": user_msg.to_dict(),
        "assistant_message": asst_msg.to_dict(),
    }


# ---------------------------------------------------------------------------
# Workflow runner(后台线程)
# ---------------------------------------------------------------------------


FREECHAT_SYSTEM_PROMPT = (
    "你是 Clawworker —— 一款基于同态加密(HE)的企业数据分析助手。"
    "用户当前的会话还没有绑定任何加密数据,所以现在是普通对话模式。\n\n"
    "请用简洁、清楚的中文回答用户的问题。\n"
    "- 如果用户问的是闲聊 / 概念解释 / 编程问题等,直接回答即可。\n"
    "- 如果用户问的是涉及具体数据的分析(例如「算一下我们公司销售额」),"
    "请告诉用户:点击输入框左侧的回形针按钮上传 CSV / XLSX 数据,系统会"
    "自动加密入库,然后再提问就会按密态数据分析模式运行。\n"
    "- 不要伪造数据或编造分析结果。"
)


def _run_workflow_for_message(sid: str, asst_mid: str, user_query: str) -> None:
    """根据会话上下文分派:自由聊天 vs 数据分析。"""
    t0 = time.time()
    _sessions.update_message(sid, asst_mid, status="running")

    sess = _sessions.get(sid)
    if not sess:
        return

    has_cipher = bool(sess.context_ciphertext and Path(sess.context_ciphertext).exists())
    has_schema = bool(sess.context_schema)

    # ----- 自由聊天模式:无密文 + 无 schema -----
    if not has_cipher and not has_schema:
        _run_freechat(sid, asst_mid, user_query, t0)
        return

    # ----- 数据分析模式 -----
    # 部分绑定 → 给提示,但不再用 emoji,文案改友好
    if not has_cipher:
        _sessions.update_message(
            sid, asst_mid,
            status="failed",
            error="本会话已设置 schema,但还差一份密文数据 · "
                  "把 CSV / XLSX 拖到对话框、或用左下角附件按钮上传即可",
            duration_sec=round(time.time() - t0, 2),
        )
        return
    if not has_schema:
        _sessions.update_message(
            sid, asst_mid,
            status="failed",
            error="已绑定密文文件,但还没设置 schema · "
                  "打开设置 → 会话 schema 粘一份 JSON,描述字段名 / 哪些是加密列",
            duration_sec=round(time.time() - t0, 2),
        )
        return

    try:
        schema = json.loads(sess.context_schema)
    except json.JSONDecodeError as e:
        _sessions.update_message(
            sid, asst_mid, status="failed",
            error=f"schema JSON 不合法:{e}",
            duration_sec=round(time.time() - t0, 2),
        )
        return

    cipher_path = Path(sess.context_ciphertext)
    username = _session_state["username"]
    host_url = _session_state["host_url"]
    token = _session_state["token"]
    backend = _config.get("backend", "stub")
    keys = _keystore.get_paths(username)
    sk_path = keys.sk_path if keys else None
    evk_path = keys.evk_path if keys else None

    try:
        zfhe = ZFHE(backend=backend, sk_path=sk_path, evk_path=evk_path)
        wf = build_workflow(
            llm_client=HTTPLLMClient(host_url=host_url, session_token=token),
            zfhe=zfhe,
            pandaseal=PandaSeal(backend=backend, evk_path=evk_path),
            henumpy=HENumpy(backend=backend, evk_path=evk_path),
            helearn=HELearn(backend=backend, evk_path=evk_path),
            hetorch=HETorch(backend="stub", evk_path=evk_path),
            authorizer=AutoApproveAuthorizer(),
        )
        final = wf.invoke({
            "user_query": user_query,
            "schema": schema,
            "ciphertext_paths": [str(cipher_path)],
        })
        summary = (final.get("summary_filtered") or final.get("summary") or "").strip()
        excel_path = final.get("excel_path") or ""
        excel_name = Path(excel_path).name if excel_path else ""

        plan = final.get("plan")
        scenario = ""
        plan_summary = ""
        if plan is not None:
            try:
                scenario = str(getattr(plan, "scenario", "")).replace("Scenario.", "")
                ops = getattr(plan, "ops", []) or []
                plan_summary = " → ".join(getattr(o, "op", str(o)) for o in ops)[:200]
            except Exception:
                pass

        if final.get("error"):
            _sessions.update_message(
                sid, asst_mid,
                status="failed",
                error=str(final["error"]),
                summary=summary,
                excel_path=str(excel_path) if excel_path else "",
                excel_name=excel_name,
                scenario=scenario,
                plan_summary=plan_summary,
                duration_sec=round(time.time() - t0, 2),
            )
        else:
            _sessions.update_message(
                sid, asst_mid,
                status="done",
                summary=summary,
                excel_path=str(excel_path) if excel_path else "",
                excel_name=excel_name,
                scenario=scenario,
                plan_summary=plan_summary,
                duration_sec=round(time.time() - t0, 2),
            )
    except Exception as e:
        _sessions.update_message(
            sid, asst_mid,
            status="failed",
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}",
            duration_sec=round(time.time() - t0, 2),
        )


def _run_freechat(sid: str, asst_mid: str, user_query: str, t0: float) -> None:
    """自由聊天 —— 直接走主机 /llm/freechat,纯文本进 / 纯文本出。"""
    host_url = _session_state["host_url"]
    token = _session_state["token"]
    try:
        r = httpx.post(
            f"{host_url}/llm/freechat",
            headers={"Authorization": f"Bearer {token}"},
            json={"system": FREECHAT_SYSTEM_PROMPT, "user": user_query},
            timeout=120,
        )
    except httpx.HTTPError as e:
        _sessions.update_message(
            sid, asst_mid, status="failed",
            error=f"无法连接主机:{type(e).__name__}: {e}",
            duration_sec=round(time.time() - t0, 2),
        )
        return
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", "")
        except Exception:
            detail = r.text[:200]
        _sessions.update_message(
            sid, asst_mid, status="failed",
            error=f"主机拒绝({r.status_code}):{detail}",
            duration_sec=round(time.time() - t0, 2),
        )
        return
    try:
        text = (r.json().get("text") or "").strip()
    except Exception:
        text = r.text
    _sessions.update_message(
        sid, asst_mid,
        status="done",
        summary=text or "(空回复)",
        scenario="freechat",
        plan_summary="自由聊天 · 无 HE 计算",
        duration_sec=round(time.time() - t0, 2),
    )


# ---------------------------------------------------------------------------
# Excel 下载
# ---------------------------------------------------------------------------


@app.get("/api/excel/download")
def api_excel_download(path: str):
    if not _is_logged_in():
        return _need_login()
    p = Path(path)
    if not p.exists() or not p.is_file():
        raise HTTPException(404, "Excel 文件不存在")
    # 校验:必须落在 Downloads / agent-system 目录里(B6-2 白名单)
    allowed_roots = [Path.home() / "Downloads", APP_DATA_DIR]
    if not any(p.resolve().is_relative_to(r.resolve()) for r in allowed_roots if r.exists()):
        raise HTTPException(403, "拒绝下载白名单外的文件")
    return FileResponse(
        p,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=p.name,
    )
