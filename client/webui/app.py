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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote

import ssl

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
from client.webui import writer as writer_mod
from client.webui.notices import NoticeStore
from client.webui.sessions import ChatSession, Message, SessionStore
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
_missed_store = MissedRunStore()
_notice_store = NoticeStore()

# 启动时登记所有已存在任务的输出文件夹根 → Excel 白名单(每任务专属输出夹)
for _t in _task_store.all_enabled():
    if getattr(_t, "output_folder", ""):
        writer_mod.register_output_root(_t.output_folder)


# ----------------------------------------------------------------------------
# FastAPI app
# ----------------------------------------------------------------------------

app = FastAPI(title="agent-system client", version="0.4.0")
app.mount("/static", StaticFiles(directory=str(_WEBUI_DIR / "static")), name="static")

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
    with _lock:
        _session_state.up…20430 tokens truncated…安装 croniter")
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
    allowed_roots = [Path.home() / "Downloads", APP_DATA_DIR / "outputs"]
    rp = p.resolve()
    if not any(rp.is_relative_to(r.resolve()) for r in allowed_roots if r.exists()):
        raise HTTPException(403, "拒绝下载白名单外的文件")
    return FileResponse(
        p,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=p.name,
    )



