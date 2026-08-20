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
import logging
import os
import re
import secrets
import tempfile
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote

import ssl
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from client.keystore import Keystore
from client.host_client import (
    HostClient,
    HostConnectionError,
    HostResponseError,
    HostSession,
)
from client.local_storage import LocalStorage
from client.tools.crypto import ZFHE
from client.webui import pipeline as pipeline_mod
from client.webui import text_extract
from client.webui import writer as writer_mod
from client.webui.background_tasks import BackgroundTaskManager, BackgroundTaskRejected
from client.webui.notices import NoticeStore
from client.webui.sessions import ChatSession, Message, SessionStore
from client.webui.state import ThreadSafeState, atomic_write_json
from client.webui.scheduler import (
    EncryptedResultStore,
    HistoryStore,
    MissedRunStore,
    PendingStore,
    Scheduler,
    TaskStore,
)
from client.webui.skills_store import (
    CustomSkillStore,
    build_custom_skills_prompt_block,
)
from client.webui.settings_routes import build_settings_router as build_security_router
from shared.prompts import load_system_prompt
from shared.paths import APP_DATA_DIR, DOWNLOADS_DIR
from shared.storage import atomic_write_bytes, atomic_write_json
from shared.version import __version__
from shared.http_observability import (
    attach_response_headers, request_id_from_header, reset_request_id, set_request_id,
)
from shared.errors import classify_exception

# ----------------------------------------------------------------------------
# 路径 / 配置
# ----------------------------------------------------------------------------

_WEBUI_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEBUI_DIR / "templates"))

CLIENT_CONFIG_FILE = APP_DATA_DIR / "client-config.json"

_lock = threading.Lock()
_session_state = ThreadSafeState(
    {"host_url": "", "username": "", "token": "", "expires_at": ""}
)


def _current_host_session() -> HostSession:
    state = (
        _session_state.snapshot()
        if isinstance(_session_state, ThreadSafeState)
        else dict(_session_state)
    )
    return HostSession(
        base_url=str(state.get("host_url") or ""),
        token=str(state.get("token") or ""),
    )


_host_client = HostClient(_current_host_session)

# 用户取消标记 —— pipeline 线程在检查点读取此 set
_cancelled_msgs: set[str] = set()
_cancel_lock = threading.Lock()
# 这些状态说明消息已经结束,再点取消是空操作 —— 不再登记取消状态,免得留下无人回收的孤儿条目
_CANCEL_NOOP_STATUSES = frozenset({"done", "failed", "cancelled", "decrypted", "needs_cipher", "skipped"})

# 解密授权门(Human-in-the-Loop / HITL):
#   mid → "decrypt" / "keep_encrypted" / "cancel"
#   pipeline 线程阻塞等待 _decrypt_events[mid].set()
_decrypt_decisions: dict[str, str] = {}
_decrypt_events: dict[str, threading.Event] = {}
_decrypt_lock = threading.Lock()


def _load_config() -> dict[str, Any]:
    defaults = {
        "host_url": "https://127.0.0.1:8443",   # 主机端已启 TLS
        "backend": "real",
    }
    if CLIENT_CONFIG_FILE.exists():
        try:
            return {**defaults, **json.loads(CLIENT_CONFIG_FILE.read_text(encoding="utf-8"))}
        except Exception:
            pass
    return defaults


def _save_config(cfg) -> None:
    atomic_write_json(
        CLIENT_CONFIG_FILE,
        cfg.snapshot() if isinstance(cfg, ThreadSafeState) else dict(cfg),
    )


_config = ThreadSafeState(_load_config())
_storage = LocalStorage()
_keystore = Keystore()
_sessions = SessionStore()
_custom_skills = CustomSkillStore()
_task_store = TaskStore()
_pending_store = PendingStore()
_run_history = HistoryStore()
_enc_results = EncryptedResultStore()
_missed_store = MissedRunStore()
_notice_store = NoticeStore()
_background_tasks = BackgroundTaskManager(max_workers=4, max_pending=24)

# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # 路由模块导入时不启动后台线程；只有真正启动 Web 应用才恢复
    # 调度任务与输出目录白名单，关闭时等待调度线程退出。
    for task in _task_store.all_enabled():
        if getattr(task, "output_folder", ""):
            writer_mod.register_output_root(task.output_folder)
    scheduler = globals().get("_scheduler")
    _background_tasks.start()
    if scheduler is not None:
        scheduler.start()
    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.stop()
        _background_tasks.close()
        _host_client.close()


app = FastAPI(title="Clawworker Enterprise Client", version=__version__, lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=str(_WEBUI_DIR / "static")), name="static")


@app.middleware("http")
async def _request_observability(request: Request, call_next):
    request_id = request_id_from_header(request.headers.get("x-request-id", ""))
    request.state.request_id = request_id
    token = set_request_id(request_id)
    try:
        response = await call_next(request)
        if response.status_code >= 500:
            logging.getLogger("clawworker").error(
                "request_id=%s code=internal_error status=%s path=%s",
                request_id, response.status_code, request.url.path,
            )
        return attach_response_headers(response, request_id)
    except Exception as exc:
        classified = classify_exception(exc)
        logging.getLogger("clawworker").exception(
            "request_id=%s code=%s category=%s path=%s",
            request_id, classified.code, classified.category.value, request.url.path,
        )
        raise
    finally:
        reset_request_id(token)

# ---- CSRF / DNS-rebinding 防护 ----------------------------------------------
# 客户端只监听 127.0.0.1,鉴权是进程内全局 session。恶意网页可对 127.0.0.1:8444
# 发跨站请求冒用已登录身份(CSRF),或用 DNS-rebinding 绕过同源。两道防线:
#   1) Host 头允许名单 —— 拦 DNS-rebinding(浏览器仍带攻击者域名的 Host)
#   2) 改状态请求需带 X-CSRF-Token 自定义头 —— 跨站无法设自定义头(会触发 CORS 预检被拒)
_CSRF_TOKEN = secrets.token_urlsafe(32)
_ALLOWED_HOST_NAMES = {"127.0.0.1", "localhost", "[::1]", "::1"}
# 表单登录/重新信任(尚无 session,可能登录被证书变更阻断)靠 Host+Origin 兜底
# /api/host/scan 在**登录页**上使用(此时还没有 session、页面也拿不到 CSRF token),
# 故豁免 token;但它仍受 Host 头与 Origin 校验保护,且只读不改状态。
_CSRF_EXEMPT_PATHS = {"/login", "/logout", "/host-trust/repin", "/api/host/scan"}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _host_name_ok(host_header: str) -> bool:
    if not host_header:
        return False
    name = host_header.rsplit(":", 1)[0].strip().lower() if "]" not in host_header \
        else host_header.rsplit("]", 1)[0].strip("[").lower()
    return name in _ALLOWED_HOST_NAMES


def _origin_ok(request: Request) -> bool:
    """Origin/Referer 若存在,其 host 必须是本机允许名单(缺失则放行,交给 CSRF token 兜底)。"""
    from urllib.parse import urlparse
    for hdr in ("origin", "referer"):
        v = request.headers.get(hdr)
        if v:
            try:
                h = urlparse(v).hostname or ""
            except ValueError:
                return False
            if h.lower() not in _ALLOWED_HOST_NAMES:
                return False
    return True


@app.middleware("http")
async def _csrf_guard(request: Request, call_next):
    # 1) Host 头:拦 DNS-rebinding(对所有请求)
    if not _host_name_ok(request.headers.get("host", "")):
        return JSONResponse({"detail": "非法 Host 头(拒绝跨域/rebinding 访问)"}, status_code=403)
    path = request.url.path
    method = request.method.upper()
    if method not in _SAFE_METHODS and not path.startswith("/static/"):
        # 2) 所有改状态请求(含登录/登出)都校验 Origin —— 挡登录 CSRF
        #    (跨站强制受害者登入攻击者主机后把数据发往攻击者)
        if not _origin_ok(request):
            return JSONResponse({"detail": "跨站来源被拒绝"}, status_code=403)
        # 3) 非豁免路径还需 CSRF token(登录表单尚无 session,仅靠上面的 Origin 兜底)
        if path not in _CSRF_EXEMPT_PATHS \
                and request.headers.get("x-csrf-token", "") != _CSRF_TOKEN:
            return JSONResponse({"detail": "CSRF 校验失败,请刷新页面重试"}, status_code=403)
    return await call_next(request)


def _is_logged_in() -> bool:
    return bool(_session_state.get("token") and _session_state.get("username"))


def _session_fresh() -> bool:
    """会话是否仍在主机核验有效期内(session expires_at 未过)。

    解密是把密文还原成明文的敏感动作。客户端本地持有 sk,解密纯本地发生 ——
    主机无法实时阻止离线解密。用会话 TTL 作「短 TTL 强制回主机」的吊销闭环:
    过期会话必须先回主机重新登录,主机在登录时应用吊销/禁用(见 host user_manager.login),
    从而把「离线可无限解密」的窗口收敛到会话 TTL(默认 8h)。见 docs/revocation-model.md。
    """
    exp = _session_state.get("expires_at")
    if not exp:
        return False
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(str(exp))
    except (ValueError, TypeError):
        return False
    now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
    return now <= dt


def _validate_host_url(raw: str) -> str:
    """
    规范化并校验主机地址。主机是企业内网/本机的控制面 —— 只允许连**私网/回环**地址,
    拒绝公网主机,防 CSRF 把 host_url 改指向攻击者服务器后把账号口令/token 送出去。
    返回补好协议的 host_url;非法则抛 ValueError。
    """
    import ipaddress
    from urllib.parse import urlparse

    url = (raw or "").strip().rstrip("/")
    if not url:
        raise ValueError("主机地址不能为空")
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url            # 用户常只填 IP:端口
    parsed = urlparse(url)
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("主机地址格式不正确")
    try:
        parsed.port
    except ValueError:
        raise ValueError("主机端口格式不正确")
    if parsed.username or parsed.password:
        raise ValueError("主机地址不能包含用户名或密码")
    # 只允许**字面 IP** 或 localhost —— 拒绝主机名。
    # 原因:主机名要 DNS 解析,而校验时解析和 httpx 请求时解析是两次(TOCTOU),
    # 攻击者可让域名先解私网(过校验)、请求时再解公网(DNS-rebinding)把口令/token 送外。
    # 字面 IP 无解析歧义;localhost 由 hosts 文件固定指向回环,不可被 DNS 操纵。
    if host.lower() != "localhost":
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError(f"只允许填**内网 IP** 或 localhost,不接受主机名「{host}」(防 DNS 劫持)")
        # IPv4-mapped IPv6(::ffff:a.b.c.d)先归一到 IPv4 再判,避免旧版误判
        if getattr(ip, "ipv4_mapped", None) is not None:
            ip = ip.ipv4_mapped
        # 显式拒 link-local —— 169.254.169.254 是云元数据端点(某些 Python 版本 is_private
        # 也含 link-local,故单独判),放行会被 SSRF 窃取实例凭证
        if ip.is_link_local:
            raise ValueError(f"拒绝链路本地/元数据地址「{host}」")
        if not (ip.is_private or ip.is_loopback):
            raise ValueError(f"只允许连内网/本机主机,拒绝地址「{host}」")
    from client import host_trust
    return host_trust.to_lan_https(url)


def _format_login_failure(host_url: str, detail: str) -> str:
    """把认证失败和实际连接的管理端绑定展示，避免多主机时误判为网络故障。"""
    hint = ""
    if detail == "账户不存在":
        hint = "。请使用在该管理端“用户管理”中创建的用户账号（不是管理端登录账号）"
    elif detail == "密码错误":
        hint = "。请在该管理端“用户管理”中重置此用户的密码后重试"
    return f"登录失败（管理端 {host_url}）：{detail}{hint}"


def _need_login() -> JSONResponse:
    return JSONResponse({"error": "not_logged_in"}, status_code=401)


def _need_revalidate() -> JSONResponse:
    """会话超过有效期,解密前须回主机重新登录核验(吊销闭环)。"""
    return JSONResponse(
        {"error": "session_stale",
         "detail": "会话已超过有效期,为核验授权未被吊销,请重新登录后再解密。"},
        status_code=401,
    )


def _clear_local_session() -> None:
    with _lock:
        _session_state.update({"host_url": "", "username": "", "token": "", "expires_at": ""})
    _host_client.reset()


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
        static_dir = _WEBUI_DIR / "static"
        mtimes = [path.stat().st_mtime for pattern in ("*.js", "*.css")
                  for path in static_dir.glob(pattern)]
        return str(int(max(mtimes)))
    except (OSError, ValueError):
        return "0"


@app.get("/host-trust", response_class=HTMLResponse)
def host_trust_page():
    """主机证书信任核对页 —— 首次登记或证书轮换后,核对指纹一致再点重新信任。
    无需登录(证书变更会阻断登录),仅本机 + 同源可访问(中间件 Host+Origin 兜底)。"""
    from client import host_trust
    host_url = host_trust.to_lan_https(
        _config.get("host_url", "") or "https://127.0.0.1:8443"
    )
    pinned = host_trust.pinned_fingerprint(host_url) or "(尚未锁定)"
    seen = host_trust.server_fingerprint(host_url) or "(取不到 · 主机未启动?)"
    match = pinned == seen
    tip = ("✓ 指纹一致,主机可信。" if match and pinned != "(尚未锁定)"
           else "⚠ 指纹不一致或未锁定。请向管理员核对下面『主机当前指纹』无误后,再点『重新信任』。")
    return f"""<!doctype html><meta charset=utf-8><title>主机信任核对</title>
<style>body{{font:14px/1.7 system-ui;max-width:680px;margin:48px auto;padding:0 20px;color:#1f2937}}
code{{background:#f3f4f6;padding:2px 6px;border-radius:4px;font-size:12px;word-break:break-all}}
.b{{background:#2563eb;color:#fff;border:0;padding:10px 18px;border-radius:8px;font-size:14px;cursor:pointer}}
h2{{margin-bottom:4px}}</style>
<h2>主机 TLS 证书信任</h2><p>主机地址:<code>{host_url}</code></p>
<p>{tip}</p>
<p>已锁定指纹:<br><code>{pinned}</code></p>
<p>主机当前指纹:<br><code>{seen}</code></p>
<form method=post action=/host-trust/repin>
<button class=b type=submit>重新信任主机当前证书</button>
&nbsp;<a href=/login>返回登录</a></form>
<p style=color:#6b7280;font-size:12px;margin-top:24px>
只有当你已通过其它渠道(如管理员口头/后台)确认『主机当前指纹』确实是本机构主机的,才点重新信任 ——
否则可能把中间人的证书当成主机信任。</p>"""


@app.post("/host-trust/repin")
def host_trust_repin():
    """用户核对指纹后重新锁定主机当前证书。"""
    from client import host_trust
    host_url = _config.get("host_url", "") or "https://127.0.0.1:8443"
    try:
        host_trust.repin(host_url)
        _host_client.reset(host_url)
        return _flash_redirect("/login", ("info", "已重新信任主机证书,请重新登录。"))
    except Exception as e:  # noqa: BLE001
        return _flash_redirect("/host-trust", ("error", f"重新信任失败:{e}"))


# ----------------------------------------------------------------------------
# 登录 / 主页
# ----------------------------------------------------------------------------


@app.get("/healthz")
def healthz():
    """供本机桌面启动器探测；不渲染模板，也不访问管理端。"""
    return {"status": "ok", "service": "client"}


@app.get("/readyz")
def readyz():
    """用户端页面、本地存储与调度线程的就绪探针。"""
    scheduler = globals().get("_scheduler")
    scheduler_ready = bool(
        scheduler is not None
        and scheduler._thread is not None
        and scheduler._thread.is_alive()
    )
    storage_ready = _storage.ciphertext_dir.exists()
    ready = scheduler_ready and storage_ready
    body = {
        "status": "ready" if ready else "starting",
        "service": "client",
        "scheduler": "ok" if scheduler_ready else "starting",
        "storage": "ok" if storage_ready else "unavailable",
        "logged_in": _is_logged_in(),
    }
    return JSONResponse(body, status_code=200 if ready else 503)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _is_logged_in():
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "index.html",
        {"username": _session_state["username"], "asset_ver": _asset_version(),
         "csrf_token": _CSRF_TOKEN},
    )


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _is_logged_in():
        return RedirectResponse("/", status_code=303)
    default_host = _config.get("host_url", "")
    if default_host:
        try:
            # 登录页加载时即迁移旧版保存的 HTTP/:8442 地址。
            default_host = _validate_host_url(default_host)
        except ValueError:
            pass
    return templates.TemplateResponse(
        request, "login.html",
        {
            "default_host": default_host,
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
    try:
        host_url = _validate_host_url(host_url)   # 补协议 + 只允许内网/本机
    except ValueError as e:
        return _flash_redirect("/login", ("error", str(e)))
    from client import host_trust
    host_url = host_trust.to_lan_https(host_url)  # 跨机器固定走局域网 HTTPS :8443
    try:
        verify = host_trust.verify_for(host_url)  # TOFU:首连锁定主机证书,之后校验一致
    except Exception as e:  # noqa: BLE001 —— 抓不到证书(主机没起/网络)
        return _flash_redirect("/login", ("error", f"无法连接主机(取证书失败):{e}"))
    try:
        r = httpx.post(
            f"{host_url}/auth/login",
            json={"username": username, "password": password},
            timeout=15,
            verify=verify,      # 校验主机出示的正是已锁定的那张证书
            # 连主机是局域网流量,绕过系统代理(Clash 等)—— 否则代理返回空 502
            trust_env=False,
        )
    except (httpx.ConnectError, ssl.SSLError) as e:
        # 先自愈:主机换网络会因 SAN 要含新 IP 而重签,整证书指纹必变但**公钥不变**。
        # 公钥一致 = 还是那台主机的合法重签 → 续锁后重试一次,别把用户拦在门外。
        if host_trust.heal_if_same_host(host_url):
            try:
                r = httpx.post(
                    f"{host_url}/auth/login",
                    json={"username": username, "password": password},
                    timeout=15, verify=host_trust.verify_for(host_url), trust_env=False,
                )
            except (httpx.ConnectError, ssl.SSLError, httpx.HTTPError) as e2:
                return _flash_redirect("/login", ("error", f"无法连接主机:{e2}"))
        else:
            # 证书校验失败且公钥也变了 = 换了主机/疑似中间人 → 给出双指纹,指向"重新信任"
            seen = host_trust.server_fingerprint(host_url) or "?"
            pinned = host_trust.pinned_fingerprint(host_url) or "?"
            if seen != pinned:
                return _flash_redirect("/login", ("error",
                    f"⚠ 主机证书已变更且公钥也变了(可能换了主机,或存在中间人)。"
                    f"已锁定指纹 {pinned[:23]}…,当前 {seen[:23]}…。"
                    f"确认是主机方变更后,打开 /host-trust 核对指纹并重新信任。"))
            return _flash_redirect("/login", ("error", f"无法连接主机:{e}"))
    except httpx.HTTPError as e:
        return _flash_redirect("/login", ("error", f"无法连接主机:{e}"))
    if r.status_code != 200:
        try:
            msg = r.json().get("detail", r.text)
        except Exception:
            msg = r.text
        return _flash_redirect(
            "/login", ("error", _format_login_failure(host_url, str(msg)))
        )

    body = r.json()
    _host_client.reset()
    with _lock:
        _session_state.update({
            "host_url": host_url, "username": username,
            "token": body["token"], "expires_at": body["expires_at"],
        })
        _config["host_url"] = host_url
        _save_config(_config)
    _bind_runtime_vault()   # 绑定该用户 vault → 引擎用其上传的密钥/字典/授权
    return RedirectResponse("/", status_code=303)


@app.get("/api/host/trust")
def api_host_trust():
    """主机 TLS 信任状态:已锁定指纹 / 当前主机指纹 / 是否一致。供设置页展示核对。"""
    if not _is_logged_in():
        return _need_login()
    from client import host_trust
    host_url = _session_state.get("host_url", "") or _config.get("host_url", "")
    pinned = host_trust.pinned_fingerprint(host_url)
    seen = host_trust.server_fingerprint(host_url)
    return {"host_url": host_url, "pinned": pinned, "server": seen,
            "match": bool(pinned and seen and pinned == seen)}


@app.post("/api/host/scan")
def api_host_scan():
    """扫描内网找主机 —— 登录页用,免去手输 IP(主机 IP 会随网络变化)。

    **不做信任决策**:只返回候选和各自公钥指纹(SPKI)。known=true 表示公钥与本地
    已锁定的一致(可安全直连);首次配置时必须由人对照管理端指纹核对后再选。
    刻意不要求登录 —— 登录页正是需要它的地方。
    """
    from client import host_scan
    try:
        return host_scan.scan()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"扫描失败:{type(e).__name__}: {e}")


@app.post("/api/host/repin")
def api_host_repin():
    """重新信任主机当前证书(证书轮换/重装后,用户核对指纹一致后调用)。"""
    if not _is_logged_in():
        return _need_login()
    from client import host_trust
    host_url = _session_state.get("host_url", "") or _config.get("host_url", "")
    try:
        fp = host_trust.repin(host_url)
        _host_client.reset(host_url)
        return {"ok": True, "fingerprint": fp}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"重新信任失败(取证书):{type(e).__name__}: {e}")


@app.post("/logout")
def logout():
    _clear_local_session()
    return JSONResponse({"ok": True})


# ----------------------------------------------------------------------------
# /api/me /api/config /api/keys
# ----------------------------------------------------------------------------


def _host_api(method: str, path: str, *, json_body=None, timeout: float = 60.0):
    """把用户端数据库请求转发给已登录的管理端；不接触数据库凭据。"""
    if not _is_logged_in():
        return _need_login()
    try:
        return _host_client.request_json(
            method, path, json_body=json_body, timeout=timeout,
        )
    except HostConnectionError as exc:
        raise HTTPException(502, str(exc)) from exc
    except HostResponseError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc


def _update_client_config(data: dict) -> dict:
    with _lock:
        if "host_url" in data:
            _config["host_url"] = _validate_host_url(str(data["host_url"]))
        if "backend" in data and data["backend"] in ("stub", "real"):
            _config["backend"] = data["backend"]
        _save_config(_config)
    return _config.snapshot()


def _bind_runtime_vault() -> None:
    """把当前登录用户的 vault 绑给 HE 引擎,使真实初始化直接用该用户
    上传的 sk / 字典 与拉取的 user_authorization(上传即生效)。"""
    try:
        from client.tools import runtime as _rt
        u = _session_state.get("username")
        _rt.set_active_vault(_keystore.vault_path(u) if u else None)
    except Exception:  # noqa: BLE001
        pass


app.include_router(build_security_router(
    is_logged_in=_is_logged_in,
    need_login=_need_login,
    keystore=_keystore,
    session_state=_session_state,
    host_client=_host_client,
    bind_runtime_vault=_bind_runtime_vault,
    clear_local_session=_clear_local_session,
))

# ----------------------------------------------------------------------------
# /api/files
# ----------------------------------------------------------------------------


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
                return df, "csv", best, []
            return df, "csv", 0, []
        except Exception:
            raise

    # xlsx —— 多 sheet 工作簿只取"数据最丰满"的一张;其余有数据的 sheet 名单一并返回,
    # 由上层提示用户(避免"传了多表却只加密了一张"的静默数据丢失)
    all_sheets = pd.read_excel(path, sheet_name=None)
    best_name, best_score = None, -1
    nonempty = []
    for n, sh in all_sheets.items():
        if sh is None or sh.empty:
            continue
        nonempty.append(str(n))
        score = sh.shape[0] * (sh.select_dtypes(include="number").shape[1] + 1) + sh.shape[1]
        if score > best_score:
            best_name, best_score = n, score
    if best_name is None:
        first = next(iter(all_sheets))
        return all_sheets[first], str(first), 0, []
    dropped = [n for n in nonempty if n != str(best_name)]
    df = all_sheets[best_name]
    header_row = 0
    if _is_bad(df):
        raw = pd.read_excel(path, sheet_name=best_name, header=None)
        # 取前 10 行里**最像表头**的那行(字符串单元格最多),而不是"第一个凑够 2 个字符串"的。
        # 两级表头(第一行是跨列合并的年份,合并后只剩 2 个非空)会骗过"≥2 就取"的判据 →
        # 选中大标题行当表头,真表头行沦为数据行、列名变成 Unnamed:N,LLM 拿到一堆无意义字段。
        # (CSV 分支本来就是按最多字符串挑的,这里对齐同一套判据。)
        best_i, best_n = 0, -1
        for i in range(min(10, len(raw))):
            r = raw.iloc[i].tolist()
            s = sum(1 for v in r if isinstance(v, str) and v.strip())
            if s >= 2 and s > best_n:
                best_i, best_n = i, s
        header_row = best_i
        if header_row > 0:
            df = pd.read_excel(path, sheet_name=best_name, header=header_row)
    return df, str(best_name), header_row, dropped


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
        df, sheet_name, header_row, dropped_sheets = _smart_read(src_path, raw_suffix)
    except Exception as e:
        raise ValueError(f"无法解析文件:{type(e).__name__}: {e}")
    if df.empty or df.shape[1] == 0:
        raise ValueError("文件没有任何列")

    # 公式列兜底:源表把"销售收入=销量×单价 / 营业利润率=营业利润/销售收入"等派生列写成公式,
    # 若保存时没缓存计算值,openpyxl/pandas 读回来整列是空的 → 会被当空字符串列丢进 metadata,
    # 下游 AI 读到空列 → 结果全 NaN(明文为空、密文是 encrypt(0))。这里按源表公式补算,
    # 让派生列变回真实数值列,正常加密、正常参与计算。失败安全兜底(不阻断摄取)。
    formula_filled: list = []
    if raw_suffix in (".xlsx", ".xls"):
        try:
            from client.he_ops.formula_eval import fill_formula_columns
            df, formula_filled = fill_formula_columns(
                str(src_path), df, sheet_name=sheet_name, header_row=header_row,
            )
        except Exception:  # noqa: BLE001 —— 公式补算失败绝不阻断入库
            formula_filled = []

    all_cols = df.columns.tolist()
    if all(str(c).startswith("Unnamed:") for c in all_cols):
        raise ValueError("无法识别表头(所有列都是 Unnamed)")
    # 脏数据体检 + 清洗:数值列里混的 "N/A"/"-"/inf → NaN,并按"非空值可强转数字比例"
    # 重判数值/身份列(避免被一两个脏值误判成文本而丢出计算)。失败回退原始 dtype 判定。
    data_health = {}
    try:
        from client.he_ops import data_health as _dh
        df, numeric_cols, string_cols, _reports = _dh.clean_for_encryption(df)
        data_health = _dh.health_summary(_reports, len(df))
        # 疑似重复导入检测:全行重复(同一批订单传两次)会让金额/数量汇总直接翻倍。
        # 只提示不删——重复行也可能是合法记录(两笔一模一样的订单),由用户判断。
        dup = int(df.duplicated().sum())
        if dup:
            pct = dup / max(len(df), 1) * 100
            data_health["duplicate_rows"] = dup
            data_health["message"] = (
                f"{data_health.get('message', '')} · ⚠ 检测到 {dup} 行完全重复的记录"
                f"(占 {pct:.1f}%),疑似重复导入——汇总类指标可能被放大,请核对源文件"
            ).lstrip(" ·")
    except Exception:  # noqa: BLE001 —— 体检失败不阻断入库
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
        atomic_write_bytes(dst, b"")

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
    atomic_write_json(schema_dst, schema)

    # 多 sheet 工作簿只加密了 sheet_name 这一张 —— 显式告知用户其余表未入库,避免静默丢数据
    multi_sheet_warning = ""
    if dropped_sheets:
        shown = "、".join(dropped_sheets[:5]) + ("…" if len(dropped_sheets) > 5 else "")
        multi_sheet_warning = (
            f"⚠ 该文件有多个工作表,本次只加密了数据最全的「{sheet_name}」。"
            f"未入库的表:{shown}。如需分析这些表,请分别上传或拆成单表文件。"
        )

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
        "data_health": data_health,
        "formula_filled": formula_filled,
        "dropped_sheets": dropped_sheets,
        "multi_sheet_warning": multi_sheet_warning,
    }


# 企业数据库接口和本地明文收口从主入口拆出。注册位置不影响 URL；依赖通过工厂注入，
# 路由模块不会反向导入 app.py，也不会形成循环依赖。
from client.webui.routers.database import build_database_router
from client.webui.routers.files import build_files_router
from client.webui.routers.ops import build_ops_router
from client.webui.routers.sessions import build_sessions_router
from client.webui.routers.settings import build_settings_router
from client.webui.routers.skills import build_skills_router
from client.webui.services.database import (
    DatabaseAnalysisService,
)

_database_service = DatabaseAnalysisService(
    _host_api,
    _ingest_plaintext_path,
    named_temporary_file=tempfile.NamedTemporaryFile,
)
app.include_router(build_database_router(
    host_api=_host_api,
    service=_database_service,
    is_logged_in=_is_logged_in,
    need_login=_need_login,
))
app.include_router(build_files_router(
    storage=_storage,
    is_logged_in=_is_logged_in,
    need_login=_need_login,
))
app.include_router(build_ops_router(
    is_logged_in=_is_logged_in,
    need_login=_need_login,
))
app.include_router(build_skills_router(
    custom_skills=_custom_skills,
    is_logged_in=_is_logged_in,
    need_login=_need_login,
))
app.include_router(build_sessions_router(
    sessions=_sessions,
    task_store=_task_store,
    missed_store=_missed_store,
    session_for_user=lambda sid: _sess_for_user(sid),
    current_username=lambda: str(_session_state.get("username") or ""),
    is_logged_in=_is_logged_in,
    need_login=_need_login,
))
app.include_router(build_settings_router(
    session_snapshot=lambda: (
        _session_state.snapshot()
        if isinstance(_session_state, ThreadSafeState)
        else dict(_session_state)
    ),
    config_snapshot=_config.snapshot,
    update_config=_update_client_config,
    is_logged_in=_is_logged_in,
    need_login=_need_login,
))


# ----------------------------------------------------------------------------
# /api/sessions
# ----------------------------------------------------------------------------


def _sess_for_user(sid: str) -> ChatSession:
    sess = _sessions.get(sid)
    if not sess or sess.username != _session_state["username"]:
        raise HTTPException(404, "会话不存在或无权访问")
    return sess


def _validated_text_attachments(data: dict) -> list[dict[str, str]]:
    """校验文本附件及用户对“文档正文将发送给所配置模型”的明确同意。"""
    raw_text_atts = data.get("text_attachments") or []
    if not isinstance(raw_text_atts, list):
        raise HTTPException(400, "文本附件格式无效")
    has_content = any(
        isinstance(item, dict) and str(item.get("content") or "").strip()
        for item in raw_text_atts
    )
    if has_content and data.get("text_attachment_llm_consent") is not True:
        raise HTTPException(
            400,
            "发送 Word/PDF/TXT 正文前需要明确同意；Excel、CSV 和企业数据库数值仍保持加密。",
        )
    attachments: list[dict[str, str]] = []
    for item in raw_text_atts:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "").strip()
        if not content:
            continue
        attachments.append({
            "name": str(item.get("name") or "attachment")[:255],
            "content": content[:30_000],
        })
    return attachments


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
    database_source_id = (data.get("database_source_id") or "").strip()[:160]
    database_source_name = (data.get("database_source_name") or "").strip()[:200]
    if database_source_id and attached_cipher:
        raise HTTPException(400, "企业数据库与 Excel/CSV 数据附件不能在同一条消息中同时使用")
    web_search = bool(data.get("web_search"))  # 用户在输入框开了「联网搜索」
    # 文本正文会进入所配置的 LLM 上下文，服务端强制要求显式同意，不能只依赖前端提示。
    text_attachments = _validated_text_attachments(data)

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
        database_source_id=database_source_id,
        database_source_name=database_source_name,
        text_attachment_names=[t["name"] for t in text_attachments],
    )
    _sessions.append_message(sid, user_msg)
    asst = Message(id=secrets.token_hex(6), role="assistant", status="pending")
    _sessions.append_message(sid, asst)

    # 2) 后台跑 pipeline
    try:
        _background_tasks.submit(
            f"ask-{sid}-{asst.id}", _run_pipeline,
            sid, asst.id, content, attached_cipher, history_for_llm, text_attachments,
            web_search=web_search,
            database_source_id=database_source_id,
            user_mid=user_msg.id,
        )
    except BackgroundTaskRejected as exc:
        _sessions.update_message(sid, asst.id, status="failed", error=str(exc))
    return {"user_message": user_msg.to_dict(), "assistant_message": asst.to_dict()}


@app.post("/api/sessions/{sid}/messages/{mid}/cancel")
def api_messages_cancel(sid: str, mid: str):
    """用户点停止按钮 —— 标记该 mid 为已取消,pipeline 会在下一个检查点退出。"""
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    # 已到终态的消息:取消本就是空操作。若照旧记状态,没有 pipeline 线程会来回收
    # (回收只发生在 _run_pipeline 的 finally 与解密门里)→ 孤儿条目在长跑进程里只增不减。
    msg = next((m for m in sess.messages if m.id == mid), None)
    if msg is not None and msg.status in _CANCEL_NOOP_STATUSES:
        return {"ok": True, "noop": True}
    with _cancel_lock:
        _cancelled_msgs.add(mid)
    # 如果当前正卡在解密授权门 → 也把 event 唤醒
    with _decrypt_lock:
        _decrypt_decisions[mid] = "cancel"
        evt = _decrypt_events.get(mid)
    if evt:
        evt.set()
    return {"ok": True}


@app.post("/api/sessions/{sid}/messages/{mid}/wizard_done")
def api_wizard_done(sid: str, mid: str):
    """聊天向导创建成功后,把触发消息的 wizard 标记为已创建(卡片变「创建完成」并持久化)。"""
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    for m in sess.messages:
        if m.id == mid:
            w = dict(getattr(m, "wizard", {}) or {})
            w["created"] = True
            _sessions.update_message(sid, mid, wizard=w)
            return {"ok": True}
    raise HTTPException(404, "消息不存在")


@app.post("/api/sessions/{sid}/messages/{mid}/clarify")
async def api_clarify(sid: str, mid: str, request: Request):
    """歧义澄清:用户在澄清卡上做了选择。
    choice=wizard → 返回抽取好的任务槽位(前端开向导);
    choice=analyze → 清掉澄清,按"只算当前附件"重跑该消息(跳过意图识别);
    choice=free → 仅清掉澄清,用户自行重述。"""
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    data = await request.json()
    choice = (data.get("choice") or "").strip()
    msgs = sess.messages
    idx = next((i for i, m in enumerate(msgs) if m.id == mid), -1)
    if idx < 0:
        raise HTTPException(404, "消息不存在")
    _sessions.update_message(sid, mid, clarify={})   # 选了就清掉澄清卡

    if choice == "wizard":
        umsg = msgs[idx - 1] if idx > 0 and msgs[idx - 1].role == "user" else None
        query = umsg.content if umsg else ""
        try:
            wiz = pipeline_mod.extract_task_slots(
                _session_state["host_url"], _session_state["token"], query)
        except Exception:
            wiz = {"name": "", "question": query, "schedule_text": "",
                   "cron": "", "cron_readable": "", "needs_data": True}
        _sessions.update_message(sid, mid, summary="好的,来创建定时任务 👇")
        return {"ok": True, "action": "wizard", "wizard": wiz}

    if choice == "analyze":
        umsg = msgs[idx - 1] if idx > 0 and msgs[idx - 1].role == "user" else None
        if not umsg:
            raise HTTPException(400, "找不到原始问题")
        query, cipher = umsg.content, (umsg.attached_cipher or "")
        history = []
        for m in msgs[:idx - 1][-6:]:
            if m.role == "user" and m.content:
                history.append({"role": "user", "content": m.content})
            elif m.role == "assistant" and m.status == "done" and m.summary:
                history.append({"role": "assistant", "content": m.summary})
        _sessions.update_message(sid, mid, status="pending", summary="", error="")
        try:
            _background_tasks.submit(
                f"clarify-{sid}-{mid}", _run_pipeline,
                sid, mid, query, cipher, history, [],
                skip_intent=True,
                database_source_id=getattr(umsg, "database_source_id", ""),
                user_mid=umsg.id,
            )
        except BackgroundTaskRejected as exc:
            _sessions.update_message(sid, mid, status="failed", error=str(exc))
        return {"ok": True, "action": "analyze", "rerun": True}

    return {"ok": True, "action": "free"}


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
    # 吊销闭环:真正产出明文的「解密」选择须会话新鲜;过期先回主机重登(应用吊销)。
    # keep_encrypted / cancel 不产出明文,放行。
    if choice == "decrypt" and not _session_fresh():
        return _need_revalidate()
    with _decrypt_lock:
        _decrypt_decisions[mid] = choice
        evt = _decrypt_events.get(mid)
    if evt:
        evt.set()
    return {"ok": True, "choice": choice}


@app.post("/api/sessions/{sid}/messages/{mid}/decrypt_file")
def api_decrypt_file(sid: str, mid: str):
    """「保留密文」后用户点「解密」:把加密暂存的结果解出明文 Excel,回填到该消息。"""
    if not _is_logged_in():
        return _need_login()
    sess = _sess_for_user(sid)
    msg = next((m for m in sess.messages if m.id == mid), None)
    if not msg:
        raise HTTPException(404, "消息不存在")
    if msg.excel_path:
        # 已解密过 → 幂等返回
        return {"ok": True, "excel_path": msg.excel_path, "excel_name": msg.excel_name}
    if not (msg.can_decrypt and msg.dec_run_id):
        raise HTTPException(400, "该结果不支持事后解密(无加密暂存)")
    # 吊销闭环:事后解密同样须会话新鲜,过期先回主机重登核验(见 _session_fresh)
    if not _session_fresh():
        return _need_revalidate()
    try:
        from client.webui import sched_results
        dec_path = sched_results.decrypt_persisted_run_to_excel(msg.dec_run_id, msg.dec_stem or "analysis")
    except Exception as e:
        raise HTTPException(500, f"解密失败:{type(e).__name__}: {e}")
    _sessions.update_message(sid, mid, excel_path=str(dec_path), excel_name=dec_path.name)
    return {"ok": True, "excel_path": str(dec_path), "excel_name": dec_path.name}


def _run_pipeline(
    sid: str, asst_mid: str, user_query: str, attached_cipher: str,
    history: list[dict[str, str]],
    text_attachments: list[dict[str, str]],
    output_mode: str = "interactive",
    sched_task: Optional[Any] = None,   # 定时密态任务对象(用于回填 EncryptedResult)
    web_search: bool = False,           # 用户开了「联网搜索」
    skip_intent: bool = False,          # 跳过意图识别(歧义澄清后用户已明确选择,直接按选择跑)
    database_source_id: str = "",       # 本条会话选择的企业数据库数据源
    user_mid: str = "",                 # 生成数据库密文后回填到对应 user message
) -> None:
    t0 = time.time()
    _sessions.update_message(sid, asst_mid, status="running")
    steps: list[dict[str, Any]] = []

    def on_step(kind: str, label: str):
        steps.append({"kind": kind, "label": label})
        _sessions.update_message(sid, asst_mid, steps=list(steps))

    # 决定用哪份 cipher:这条消息带的 > 上一条 user 消息的(沿用)。
    # 注意:是否真的"用"这份密文,由意图决定 —— 只有"对数据分析"的意图才会用它;
    #       问天气/新闻/概念等会走自由聊天(见 pipeline.ask 的意图路由),与是否沿用无关。
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

    # 意图识别(普通会话、交互式、未要求跳过时):
    #   ① 矛盾/歧义(如"定时"措辞 + 带了附件)→ 先弹澄清卡让用户选,不擅自决定
    #   ② 无歧义的排程意图 → 直接弹"创建定时任务"向导
    if (output_mode == "interactive"
            and sess is not None and getattr(sess, "kind", "normal") != "scheduled"
            and not skip_intent):
        # 歧义只看**本条消息**是否带了附件(沿用的旧密文不算)——否则做过分析的会话里
        # 再说排程会被误判成"定时 vs 只算这个附件",把创建向导卡挡掉。
        this_msg_attached = bool(
            (attached_cipher and Path(attached_cipher).exists()) or database_source_id
        )
        amb = pipeline_mod.detect_intent_ambiguity(user_query, has_attachment=this_msg_attached)
        if amb:
            _sessions.update_message(
                sid, asst_mid, status="done",
                summary="我需要先跟你确认一下 👇",
                clarify=amb, duration_sec=round(time.time() - t0, 2),
            )
            return
        if pipeline_mod.looks_like_schedule_request(user_query):
            try:
                wiz = pipeline_mod.extract_task_slots(
                    _session_state["host_url"], _session_state["token"], user_query)
            except Exception:
                wiz = {"name": "", "question": user_query, "schedule_text": "",
                       "cron": "", "cron_readable": "", "needs_data": True}
            _sessions.update_message(
                sid, asst_mid, status="done",
                summary="看起来你想创建一个**定时任务**,我来帮你一步步设置 👇",
                wizard=wiz, duration_sec=round(time.time() - t0, 2),
            )
            return

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
        if database_source_id:
            db_cipher = _database_service.prepare_cipher(
                data_source_id=database_source_id,
                intent=user_query,
                on_step=on_step,
                should_cancel=_should_cancel,
            )
            if db_cipher.get("cancelled"):
                _sessions.update_message(
                    sid, asst_mid, status="cancelled", summary="已停止 · 数据库查询已取消",
                    duration_sec=round(time.time() - t0, 2),
                )
                return
            cipher_path = Path(db_cipher["path"])
            used_cipher = str(cipher_path)
            if user_mid:
                _sessions.update_message(sid, user_mid, attached_cipher=used_cipher)
            on_step(
                "encrypt",
                f"数据库数据已在本机加密（{db_cipher.get('row_count', 0)} 行），开始密态分析",
            )
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
            # 定时任务:固化首次成功生成的代码,每次到点复用 → 输出结构一致
            codegen_cache_key=(f"task_{sched_task.id}" if sched_task is not None else ""),
            web_search=web_search,
            audit_user=_session_state.get("username", ""),
            audit_session=sid,
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
        # 不管成功失败都把该消息的临时状态清干净,避免泄漏。
        # _decrypt_decisions 也要清:取消一个正在跑的分析时,cancel 端点会写入这条,
        # 而若 pipeline 在走到解密门之前就退出,就没人来 pop 它了。
        with _cancel_lock:
            _cancelled_msgs.discard(asst_mid)
        with _decrypt_lock:
            _decrypt_decisions.pop(asst_mid, None)
            _decrypt_events.pop(asst_mid, None)

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
        _record_sched_outcome(sched_task, ok=False, err=err)  # 定时任务失败熔断计数
        return

    # 定时密态:结果加密暂存 → 累积成 EncryptedResult(按任务聚合,待批量解密)
    if status == "encrypted_pending":
        enc = result.get("encrypted_run") or {}
        run_summary = result.get("summary", "")
        if sched_task is not None and enc.get("manifest"):
            _enc_results.add(
                username=sched_task.username, task_id=sched_task.id,
                task_name=sched_task.name, run_id=enc.get("run_id", run_id),
                run_at=datetime.now().isoformat(timespec="seconds"),
                question=sched_task.question, manifest=enc.get("manifest", []),
            )
            # 每任务专属输出夹:把密文版 Excel 落到 <output_folder>/密文/(未授权也可见/留存)
            of = getattr(sched_task, "output_folder", "")
            if of:
                try:
                    from client.webui import sched_results
                    writer_mod.register_output_root(of)
                    p = sched_results.export_encrypted_run_to_folder(
                        enc.get("run_id", run_id), enc.get("manifest", []),
                        of, stem=sched_task.name,
                        run_at=datetime.now().isoformat(timespec="seconds"))
                    if p:
                        run_summary += f" · 密文已存入 {Path(of).name}/密文/"
                except Exception as e:  # noqa: BLE001 —— 落密文夹失败不影响沙盒暂存与后续解密
                    run_summary += f" · (密文落盘失败:{type(e).__name__})"
        _sessions.update_message(
            sid, asst_mid, status="done",
            summary=run_summary,
            skill_calls=result.get("skill_calls", []),
            used_cipher=used_cipher,
            duration_sec=round(time.time() - t0, 2),
            tokens=int(result.get("tokens", 0) or 0),
        )
        _record_sched_outcome(sched_task, ok=True)   # 成功 → 清零连续失败计数
        return

    excel_path = result.get("excel_path", "")
    enc_path = result.get("enc_excel_path", "")
    _sessions.update_message(
        sid, asst_mid, status="done",
        summary=result.get("summary", ""),
        excel_path=excel_path,
        excel_name=result.get("excel_name") or (Path(excel_path).name if excel_path else ""),
        enc_excel_path=enc_path,
        enc_excel_name=result.get("enc_excel_name") or (Path(enc_path).name if enc_path else ""),
        can_decrypt=bool(result.get("can_decrypt")),
        dec_run_id=result.get("dec_run_id", ""),
        dec_stem=result.get("dec_stem", ""),
        skill_calls=result.get("skill_calls", []),
        used_cipher=used_cipher,
        duration_sec=round(time.time() - t0, 2),
        tokens=int(result.get("tokens", 0) or 0),
    )


# ----------------------------------------------------------------------------
# 定时任务(MVP)
# ----------------------------------------------------------------------------


def _ensure_task_session(task) -> str:
    """确保任务有一个聊天会话(累积它的历次运行);返回 session_id。"""
    sid = task.session_id
    sess = _sessions.get(sid) if sid else None
    if not sess:
        sess = _sessions.create(username=task.username, title=f"⏰ {task.name}",
                                kind="scheduled", task_id=task.id)
        _task_store.update(task.id, session_id=sess.id)
        sid = sess.id
    return sid


def _launch_run(*, username: str, task_name: str, question: str,
                cipher_path: str, session_id: str,
                output_mode: str = "interactive", sched_task=None,
                web_search: bool = False, note: str = "") -> None:
    """把一次运行注入聊天会话并跑 pipeline。output_mode=encrypted_sandbox 时密态结果加密暂存。
    note 非空(漏跑补救)→ 附在该轮助手消息执行时间下方,与这轮对话同属一个整体。"""
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
    asst = Message(id=secrets.token_hex(6), role="assistant", status="pending",
                   remediation_note=note or "")
    _sessions.append_message(session_id, asst)
    try:
        _background_tasks.submit(
            f"sched-{session_id}-{asst.id}", _run_pipeline,
            session_id, asst.id, question, cipher_path or "", history_for_llm, [],
            output_mode=output_mode, sched_task=sched_task, web_search=web_search,
        )
    except BackgroundTaskRejected as exc:
        _sessions.update_message(session_id, asst.id, status="failed", error=str(exc))


# 源文件夹加密入库缓存:同一文件(路径+mtime)只加密一次,供同文件夹的多个任务复用
_FOLDER_INGEST_CACHE_MAX = 128
_folder_ingest_cache: OrderedDict[tuple, str] = OrderedDict()
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
            if cached:
                _folder_ingest_cache.move_to_end(key)
        if cached and Path(cached).exists():
            return cached, f"复用已加密的最新文件:{latest.name}"
        try:
            info = _ingest_plaintext_path(latest, latest.name)
        except Exception as e:
            return None, f"最新文件加密入库失败:{e}"
        with _folder_ingest_lock:
            _folder_ingest_cache[key] = info["path"]
            _folder_ingest_cache.move_to_end(key)
            while len(_folder_ingest_cache) > _FOLDER_INGEST_CACHE_MAX:
                _folder_ingest_cache.popitem(last=False)
        return info["path"], f"已加密最新文件:{latest.name}"
    if task.cipher_path:
        return task.cipher_path, ""
    return None, ""


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


_REL_DATE_RE = re.compile(r"今日|今天|本日|当日|当天|现在|目前|此刻")


def _date_adjust_question(question: str, due_at: str) -> str:
    """漏跑补跑时,把问题里的相对日期(今日/今天/现在 · 昨天/明天/后天)替换成
    漏跑当天对应的实际日期,避免「今日上海天气」补跑成今天的(应是漏跑那天的)。"""
    if not question or not due_at:
        return question
    try:
        d = datetime.fromisoformat(due_at)
    except Exception:
        return question

    def lbl(delta: int) -> str:
        dd = d + timedelta(days=delta)
        return f"{dd.month}月{dd.day}日"

    q = question
    # 先处理带"今/天"易冲突的相对词(顺序:多日差的先替换)
    for word, delta in (("前天", -2), ("后天", 2), ("昨天", -1), ("明天", 1)):
        q = q.replace(word, lbl(delta))
    q = _REL_DATE_RE.sub(lbl(0), q)   # 今日/今天/现在… → 漏跑当天
    return q


def _fmt_due(due_at: str) -> str:
    return (due_at or "")[:16].replace("T", " ") or "—"


def _append_event(session_id: str, kind: str, text: str) -> None:
    """把一条系统事件(漏跑 / 忽略 / 补救)写进任务会话,留痕、可在会话里查看。"""
    if not session_id:
        return
    try:
        _sessions.append_message(session_id, Message(
            id=secrets.token_hex(6), role="event", event_kind=kind,
            content=text, status="done"))
    except Exception:
        pass


def _record_missed(task, due_at: str, reason: str) -> None:
    """记一条漏跑预警(去重:同任务同到点窗口只记一条 pending),并把漏跑事件写进任务会话。"""
    m = None
    try:
        m = _missed_store.add_deduped(
            username=task.username, task_id=task.id, task_name=task.name,
            question=task.question, due_at=due_at or "", reason=reason,
            needs_data=bool(task.needs_approval),
        )
    except Exception:
        m = None
    if m is not None:   # 新漏跑(非重复)→ 写入任务会话
        try:
            sid = _ensure_task_session(task)
            _append_event(sid, "missed",
                          f"⚠ 漏跑:本应于 {_fmt_due(due_at)} 执行的任务未运行 —— {reason}")
        except Exception:
            pass


def _on_scheduler_miss(task, due_dt) -> None:
    """调度器检测到漏跑(服务当时没运行,到点窗口已过去很久)→ 预警,不自动补跑。"""
    due_at = due_dt.isoformat(timespec="seconds") if hasattr(due_dt, "isoformat") else str(due_dt)
    _record_missed(task, due_at, "设定时间未执行(服务当时未运行)· 可手动补救")
    _run_history.add(
        username=task.username, task_id=task.id, task_name=task.name,
        ran_at=_now_iso(), status="missed",
        summary=f"漏跑:{due_at} 该轮未执行(服务未运行)· 已生成预警",
    )


_SCHED_FAIL_DISABLE_AT = 3   # 连续失败达此次数 → 自动暂停任务并告警


def _record_sched_outcome(sched_task, *, ok: bool, err: str = "") -> None:
    """
    记定时任务运行结果:成功清零连续失败;失败累加,达阈值自动暂停并发通知。
    解决"定时任务每期静默失败、界面全 0、用户以为正常产出"的问题。
    """
    if sched_task is None:
        return
    try:
        t = _task_store.get(sched_task.id)
        if not t:
            return
        if ok:
            if getattr(t, "fail_streak", 0):
                _task_store.update(sched_task.id, fail_streak=0, auto_paused_reason="")
            return
        streak = int(getattr(t, "fail_streak", 0)) + 1
        if streak >= _SCHED_FAIL_DISABLE_AT:
            # 连续失败达阈值 → 自动暂停,避免每天静默失败刷屏且浪费算力
            _task_store.update(sched_task.id, fail_streak=streak, enabled=False,
                               auto_paused_reason=f"连续 {streak} 次失败已自动暂停")
            _notice_store.add(
                username=t.username, key=f"schedfail:{t.id}:{streak}", level="error",
                title=f"定时任务已自动暂停 · {t.name}",
                summary=(f"任务「{t.name}」连续 {streak} 次运行失败,已自动暂停以免持续空跑。"
                         f"最近错误:{err[:120]}。修正后到「定时任务管理」重新启用。"),
                created_at=_now_iso())
        else:
            _task_store.update(sched_task.id, fail_streak=streak)
            _notice_store.add(
                username=t.username, key=f"schedfail:{t.id}:{streak}", level="warning",
                title=f"定时任务运行失败 · {t.name}",
                summary=(f"任务「{t.name}」第 {streak} 次失败:{err[:120]}。"
                         f"连续 {_SCHED_FAIL_DISABLE_AT} 次将自动暂停。"),
                created_at=_now_iso())
    except Exception:  # noqa: BLE001 —— 熔断记账失败绝不影响主流程
        pass


def _on_scheduler_fire(task) -> None:
    """调度器到点回调(在 scheduler 线程里)。"""
    # 密态分析(有数据:固定密文 / 源文件夹)→ 正常计算,结果加密暂存,累积待批量解密
    if task.needs_approval:
        if not (_is_logged_in() and _session_state.get("username") == task.username):
            # 仅活跃会话:没登录就不跑 → 记漏跑预警(可手动补救)
            _run_history.add(
                username=task.username, task_id=task.id, task_name=task.name,
                ran_at=datetime.now().isoformat(timespec="seconds"),
                status="skipped", summary="到点时未登录 · 跳过(仅活跃会话)",
            )
            _record_missed(task, task.next_run or _now_iso(),
                           "到点时未登录,该轮未执行")
            return
        cipher_path, note = _resolve_task_cipher(task)
        if not cipher_path:
            _run_history.add(
                username=task.username, task_id=task.id, task_name=task.name,
                ran_at=datetime.now().isoformat(timespec="seconds"),
                status="skipped", summary=note or "无可用数据 · 跳过",
            )
            _record_missed(task, task.next_run or _now_iso(),
                           note or "无可用数据,该轮未执行")
            return
        sid = _ensure_task_session(task)
        _launch_run(username=task.username, task_name=task.name,
                    question=task.question, cipher_path=cipher_path,
                    session_id=sid, output_mode="encrypted_sandbox", sched_task=task,
                    web_search=bool(getattr(task, "web_search", False)))
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
                    question=task.question, cipher_path="", session_id=sid,
                    web_search=bool(getattr(task, "web_search", False)))
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


_scheduler = Scheduler(_task_store, _on_scheduler_fire, poll_seconds=30,
                       on_miss=_on_scheduler_miss, miss_grace_seconds=600)


@app.get("/api/scheduled_tasks")
def api_tasks_list():
    if not _is_logged_in():
        return _need_login()
    u = _session_state["username"]
    return {
        "tasks": [t.to_dict() for t in _task_store.list_for(u)],
        "pending_count": (_pending_store.count_pending(u)
                          + _enc_results.count_pending(u)
                          + _missed_store.count_pending(u)),
        "missed_count": _missed_store.count_pending(u),
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
    output_folder = (data.get("output_folder") or "").strip()
    if output_folder:
        od = Path(output_folder).expanduser()
        try:
            od.mkdir(parents=True, exist_ok=True)   # 输出夹不存在则创建(用户指定的落盘位置)
        except Exception as e:
            raise HTTPException(400, f"输出文件夹无法创建:{output_folder}({e})")
        output_folder = str(od)
        writer_mod.register_output_root(output_folder)
    t = _task_store.create(
        username=_session_state["username"], name=name, question=question,
        cipher_path=cipher_path, source_folder=source_folder, source_pattern=source_pattern,
        output_folder=output_folder,
        web_search=bool(data.get("web_search", False)),
        schedule_kind=kind, cron_expr=cron_expr,
        cron_readable=(data.get("cron_readable") or "").strip(),
        interval_minutes=int(data.get("interval_minutes", 60) or 60),
        at_hour=int(data.get("at_hour", 9) or 0),
        at_minute=int(data.get("at_minute", 0) or 0),
        weekday=int(data.get("weekday", 0) or 0),
        day_of_month=int(data.get("day_of_month", 1) or 1),
        enabled=bool(data.get("enabled", True)),
    )
    # 立刻建好该任务的会话(kind=scheduled),使其马上出现在「定时任务」会话列表里
    _ensure_task_session(t)
    return _task_store.get(t.id).to_dict()


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
                                  "cipher_path", "source_folder", "source_pattern",
                                  "output_folder", "web_search") if k in data}
    if patch.get("output_folder"):
        od = Path(patch["output_folder"]).expanduser()
        try:
            od.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            raise HTTPException(400, f"输出文件夹无法创建:{patch['output_folder']}({e})")
        patch["output_folder"] = str(od)
        writer_mod.register_output_root(patch["output_folder"])
    t = _task_store.update(tid, **patch)
    # 任务改名 → 同步更新它绑定的会话标题(左侧子任务名立刻跟着变)
    if "name" in patch and t and t.session_id and _sessions.get(t.session_id):
        _sessions.rename(t.session_id, f"⏰ {t.name}")
    return t.to_dict()


@app.delete("/api/scheduled_tasks/{tid}")
def api_tasks_delete(tid: str):
    if not _is_logged_in():
        return _need_login()
    t = _task_store.get(tid)
    if not t or t.username != _session_state["username"]:
        raise HTTPException(404, "任务不存在")
    sid = getattr(t, "session_id", "") or ""
    _task_store.delete(tid)
    pipeline_mod._codegen_cache_delete(f"task_{tid}")  # 任务删了,固化代码缓存一并清
    # 关联记录一并清:待批 / 密态结果 / 漏跑 / 运行历史 + 该任务的聊天会话
    _pending_store.delete_for_task(tid)
    _enc_results.delete_for_task(tid)
    _missed_store.delete_for_task(tid)
    _run_history.delete_for_task(tid)
    if sid and _sessions.get(sid):
        _sessions.delete(sid)
    return {"ok": True, "session_id": sid}


@app.post("/api/scheduled_tasks/{tid}/session")
def api_task_session(tid: str):
    """「查看会话」:确保该任务有一个聊天会话(历次运行累积在此),返回 session_id。
    会话历史持久化在沙盒,关闭/切走都不清除,删除任务时才一并清理。"""
    if not _is_logged_in():
        return _need_login()
    t = _task_store.get(tid)
    if not t or t.username != _session_state["username"]:
        raise HTTPException(404, "任务不存在")
    sid = _ensure_task_session(t)
    _sessions.set_hidden(sid, False)   # 若之前被软隐藏,重新显示并带回全部已运行内容
    return {"session_id": sid}


# ----------------------------------------------------------------------------
# 站内信(只读通知 + 留痕)—— 从既有 store 派生,读取时同步(天然补发停机期间的消息)
# ----------------------------------------------------------------------------

def _fmt_dt(iso: str) -> str:
    """ISO 时间 → 'YYYY-MM-DD HH:MM'(给人看);解析不了就原样返回。"""
    s = (iso or "").replace("T", " ")
    return s[:16] if len(s) >= 16 else (s or "—")


def _sync_notices(username: str) -> None:
    """把底层持久记录(漏跑 / 待解密 / 会话执行失败)同步成站内信。
    幂等:同一来源事件只生成一条(去重 key);服务中断期间的事件,恢复后首次同步即补出。"""
    seen = _notice_store.seen_keys(username)

    # 1) 漏跑(未处理)→ warning
    for m in _missed_store.list_pending(username):
        key = f"missed:{m.id}"
        if key in seen:
            continue
        how = "需先选择该轮数据文件再补跑" if getattr(m, "needs_data", False) else "可直接补跑"
        _notice_store.add(
            username=username, key=key, level="warning",
            title=f"定时任务漏跑 · {m.task_name}",
            summary=(f"任务「{m.task_name}」原定 {_fmt_dt(m.due_at)} 执行,但未跑成:{m.reason}。"
                     f"该任务{how} —— 到「定时任务管理 → 漏跑」处理。"),
            created_at=m.due_at or m.created_at)

    # 2) 待解密就绪(按任务聚合,一个任务一条)→ info
    for a in _enc_results.aggregate_by_task(username):
        key = f"enc:{a['task_id']}"
        if key in seen:
            continue
        _notice_store.add(
            username=username, key=key, level="info",
            title=f"密态结果待解密 · {a['task_name']}",
            summary=(f"任务「{a['task_name']}」已有 {a['count']} 份密态结果加密暂存,"
                     f"最近一次 {_fmt_dt(a.get('latest_run', ''))}。"
                     f"授权后可批量解密为明文 Excel —— 到「定时任务管理 → 待解密文件」处理。"),
            created_at=a.get("latest_run", "") or _now_iso())

    # 3) 定时任务执行失败(含主进程重启被中断的运行)→ critical
    for sess in _sessions.list_for(username):
        if getattr(sess, "kind", "normal") != "scheduled":
            continue
        tname = (sess.title or "").replace("⏰ ", "").strip() or "定时任务"
        for msg in sess.messages:
            if msg.role != "assistant" or msg.status != "failed":
                continue
            key = f"runfail:{msg.id}"
            if key in seen:
                continue
            err = (msg.error or "未知错误").strip().replace("\n", " ")
            _notice_store.add(
                username=username, key=key, level="critical",
                title=f"定时任务执行失败 · {tname}",
                summary=(f"任务「{tname}」于 {_fmt_dt(msg.created_at)} 执行失败:{err[:240]}。"
                         f"可到该任务会话查看详情,或在「定时任务管理」里检查配置后重试。"),
                created_at=msg.created_at)


@app.get("/api/notices")
def api_notices():
    if not _is_logged_in():
        return _need_login()
    u = _session_state["username"]
    _sync_notices(u)   # 读取即同步:实时(前端轮询)+ 补发(停机期间的事件)
    return {
        "items": [n.to_dict() for n in _notice_store.list_for(u)],
        "unread": _notice_store.unread_count(u),
    }


@app.post("/api/notices/read")
def api_notices_read():
    """打开站内信即全部标记已读 → 小红点消失。"""
    if not _is_logged_in():
        return _need_login()
    u = _session_state["username"]
    _notice_store.mark_all_read(u)
    return {"ok": True, "unread": 0}


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
    # 三类:① 自由问答的待跑(PendingRun)② 密态任务的加密结果(按任务聚合)③ 漏跑预警
    runs = [dict(p.to_dict(), kind="run") for p in _pending_store.list_pending(u)]
    encrypted = [dict(a, kind="decrypt") for a in _enc_results.aggregate_by_task(u)]
    missed = [dict(m.to_dict(), kind="missed") for m in _missed_store.list_pending(u)]
    return {"runs": runs, "encrypted": encrypted, "missed": missed}


@app.post("/api/scheduled_tasks/missed/{mid}/dismiss")
def api_missed_dismiss(mid: str):
    """忽略一条漏跑预警。"""
    if not _is_logged_in():
        return _need_login()
    m = _missed_store.get(mid)
    if not m or m.username != _session_state["username"]:
        raise HTTPException(404, "预警不存在")
    _missed_store.set_status(mid, "dismissed")
    task = _task_store.get(m.task_id)
    if task is not None:
        _append_event(_ensure_task_session(task), "dismissed",
                      f"⊘ 已忽略漏跑:{_fmt_due(m.due_at)} 那轮不再补跑。")
    return {"ok": True}


@app.post("/api/scheduled_tasks/missed/{mid}/remediate")
async def api_missed_remediate(mid: str, request: Request):
    """手动补救一条漏跑:用用户指定的数据文件,把该轮重新跑一遍(密态 → 加密暂存待解密)。
    body: {cipher_path?: 已加密文件, source_path?: 本地明文文件(将加密入库)}"""
    if not _is_logged_in():
        return _need_login()
    m = _missed_store.get(mid)
    if not m or m.username != _session_state["username"]:
        raise HTTPException(404, "预警不存在")
    task = _task_store.get(m.task_id)
    data = await request.json()
    cipher_path = (data.get("cipher_path") or "").strip()
    source_path = (data.get("source_path") or "").strip()

    if m.needs_data:
        if source_path:
            sp = Path(source_path).expanduser()
            if not sp.is_file():
                raise HTTPException(400, f"指定的文件不存在:{source_path}")
            try:
                info = _ingest_plaintext_path(sp, sp.name)
                cipher_path = info["path"]
            except Exception as e:
                raise HTTPException(400, f"该轮文件加密入库失败:{e}")
        elif not cipher_path:
            raise HTTPException(400, "该任务需要数据 · 请指定本轮要处理的文件")
        if cipher_path and not Path(cipher_path).exists():
            raise HTTPException(400, "指定的密文文件不存在")

    # 注入会话并跑(数据任务 → encrypted_sandbox 累积待解密;自由问答 → 直接跑)。
    # 补跑时把问题里的相对日期(今日/今天…)锚定到漏跑当天,避免实时问题跑成今天的。
    # 补救说明作为该轮助手消息的一部分(执行时间下方),与这轮对话同属一个整体。
    note = f"✓ 手动补救:对 {_fmt_due(m.due_at)} 那轮重新执行。"
    if task is not None:
        sid = _ensure_task_session(task)
        output_mode = "encrypted_sandbox" if m.needs_data else "interactive"
        q = _date_adjust_question(task.question, m.due_at)
        _launch_run(username=m.username, task_name=task.name, question=q,
                    cipher_path=cipher_path, session_id=sid,
                    output_mode=output_mode, sched_task=task if m.needs_data else None,
                    web_search=bool(getattr(task, "web_search", False)), note=note)
    else:
        # 任务已删:用预警里存的问题在一个补救会话里跑
        sess = _sessions.create(username=m.username, title=f"⏰ 补救 · {m.task_name}",
                                kind="scheduled")
        sid = sess.id
        _launch_run(username=m.username, task_name=m.task_name,
                    question=_date_adjust_question(m.question, m.due_at),
                    cipher_path=cipher_path, session_id=sid, note=note)

    _missed_store.set_status(mid, "resolved")
    _run_history.add(
        username=m.username, task_id=m.task_id, task_name=m.task_name,
        ran_at=_now_iso(), status="launched", summary="漏跑补救 · 已手动重跑该轮 · 见会话",
    )
    return {"ok": True, "session_id": sid, "needs_approval": bool(m.needs_data)}


@app.post("/api/scheduled_tasks/decrypt/{task_id}")
def api_task_decrypt(task_id: str):
    """批量解密一个密态任务累积的所有加密结果 → 落到一个文件夹。"""
    if not _is_logged_in():
        return _need_login()
    # 吊销闭环:批量解密同样须会话新鲜,过期先回主机重登核验(见 _session_fresh)
    if not _session_fresh():
        return _need_revalidate()
    u = _session_state["username"]
    items = _enc_results.pending_for_task(task_id)
    items = [r for r in items if r.username == u]
    if not items:
        raise HTTPException(404, "该任务没有待解密的结果")
    task = _task_store.get(task_id)
    folder_name = (task.name if task else items[0].task_name) or "定时任务结果"
    output_folder = getattr(task, "output_folder", "") if task else ""
    if output_folder:
        writer_mod.register_output_root(output_folder)
    runs = [{"run_id": r.run_id, "run_at": r.run_at, "manifest": r.manifest,
             "question": r.question} for r in items]
    try:
        from client.webui import sched_results
        out_dir, outcomes = sched_results.decrypt_runs_to_folder(
            runs, folder_name, output_folder=output_folder)
    except Exception as e:
        raise HTTPException(500, f"批量解密失败:{type(e).__name__}: {e}")

    # 只对真正出了文件的 run 标记已解密 + 清沙盒密文;
    # 失败的保留待批、密文保留(此前版本整批标记 → 失败 run 数据无声丢失)
    ok_run_ids = {o["run_id"] for o in outcomes if o.get("ok")}
    ok_items = [r for r in items if r.run_id in ok_run_ids]
    failures = [o for o in outcomes if not o.get("ok")]
    if not ok_items:
        detail = failures[0].get("error", "未知原因") if failures else "未知原因"
        raise HTTPException(500, f"批量解密失败(全部 {len(items)} 次运行未产出文件):{detail}")
    _enc_results.mark_decrypted([r.id for r in ok_items])
    try:
        sched_results.cleanup_runs([r.run_id for r in ok_items])
    except Exception:
        pass
    summary = f"已批量解密 {len(ok_items)}/{len(items)} 次运行 → 文件夹 {out_dir.name}"
    if failures:
        summary += f" · {len(failures)} 次失败保留待批({failures[0].get('error', '')[:60]})"
    _run_history.add(
        username=u, task_id=task_id, task_name=folder_name,
        ran_at=datetime.now().isoformat(timespec="seconds"),
        status="decrypted", summary=summary,
    )
    return {"ok": True, "folder": str(out_dir), "count": len(ok_items),
            "failed": len(failures), "failures": failures}


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
        sess = _sessions.create(username=p.username, title=f"⏰ {p.task_name}",
                                kind="scheduled", task_id=p.task_id)
        sid = sess.id
    _task = _task_store.get(p.task_id)
    _launch_run(username=p.username, task_name=p.task_name,
                question=p.question, cipher_path=p.cipher_path, session_id=sid,
                web_search=bool(getattr(_task, "web_search", False)) if _task else False)
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
    # 扩展名白名单:只放行电子表格产出,绝不允许下载 sk.bin / accounts.json 等
    # (即便它们落在下面的目录里)——堵死"把私钥当 Excel 下载"的路径。
    if p.suffix.lower() not in (".xlsx", ".xls", ".csv"):
        raise HTTPException(403, "只允许下载电子表格文件(.xlsx/.xls/.csv)")
    # 根目录收窄:仅 Downloads + 产出暂存目录 outputs —— 不再放行整个 ~/.agent-system
    # (那里有密钥沙盒 keystore/、账户 host-auth/、审计 audit/ 等敏感数据)。
    allowed_roots = [DOWNLOADS_DIR, APP_DATA_DIR / "outputs"]
    rp = p.resolve()
    if not any(rp.is_relative_to(r.resolve()) for r in allowed_roots if r.exists()):
        raise HTTPException(403, "拒绝下载白名单外的文件")
    return FileResponse(
        p,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=p.name,
    )


