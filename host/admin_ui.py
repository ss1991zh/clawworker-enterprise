"""
企业版 Admin UI(Jinja2 模板 + FastAPI)。

简约扁平风格,4 个页面:
- /admin/                   概览(KPI + 系统状态 + LLM 调用统计)
- /admin/users              用户(证书 + 账户合并视图,含 LLM 配置绑定)
- /admin/llm                LLM 配置 CRUD(多份配置 + 模型自动探测)
- /admin/sessions           在线会话(查看 / 注销)

⚠️ MVP 阶段:没有 admin 登录鉴权,假设 admin 在主机本地访问。
   生产部署应:绑定 localhost only,或加 Basic Auth / SSO。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from host.llm_configs import (
    CallStatStore,
    LLMConfigStore,
    ProviderManager,
)
from host.notices import NoticeStore
from host.admin_sections import build_data_usage_router, build_ops_router
from host.admin_data_permissions import DataPermissionAdminService, build_data_permissions_router
from host.admin_llm import build_llm_router
from host.admin_data_sources import build_data_sources_router
from host.admin_sessions import build_sessions_router
from host.admin_users import build_users_router

# 模板目录
_HOST_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HOST_DIR / "templates"))

# Admin 站内信(全局,只读通知)
_notice_store = NoticeStore()


def _iso(dt) -> str:
    try:
        return dt.isoformat(timespec="seconds")
    except Exception:
        return ""


def _asset_ver(name: str) -> str:
    """静态资源版本号 = 文件 mtime,用于 ?v= 破缓存。
    改了 admin.css 后无需手动 bump、也不依赖用户硬刷新。"""
    try:
        return str(int((_HOST_DIR / "static" / name).stat().st_mtime))
    except OSError:
        return "0"


# 注册为 Jinja 全局,所有模板可直接 {{ asset_ver('admin.css') }}
templates.env.globals["asset_ver"] = _asset_ver


def build_admin_router(
    *,
    auth_manager,
    user_manager,
    dispatcher,
    llm_config_store: LLMConfigStore,
    provider_manager: ProviderManager,
    call_stats: CallStatStore,
    data_source_store=None,
    connector_registry=None,
    data_access_store=None,
    query_gateway=None,
    admin_auth=None,
    login_throttle=None,
) -> APIRouter:
    """构造 admin 路由 router。"""
    router = APIRouter(prefix="/admin", tags=["admin"])
    permission_admin = DataPermissionAdminService(
        data_source_store=data_source_store,
        data_access_store=data_access_store,
        user_manager=user_manager,
        query_gateway=query_gateway,
    )

    # ============================================================
    # 登录 / 退出 / 账户(密码 + 绑定邮箱 + 邮箱验证码改密)
    # ============================================================
    from host.admin_auth import COOKIE as ADMIN_COOKIE, SESSION_TTL

    @router.get("/login", response_class=HTMLResponse)
    def admin_login_form(request: Request):
        # 已登录直接进首页
        if admin_auth and admin_auth.valid(request.cookies.get(ADMIN_COOKIE)):
            return RedirectResponse("/admin/", status_code=303)
        msgs = _pop_messages(request)
        if admin_auth and admin_auth.is_default_password():
            msgs = list(msgs) + [("warn", "当前仍在使用默认口令 admin/123456,登录后请立即到「账户」修改!")]
        return templates.TemplateResponse(
            request, "admin_login.html",
            {"messages": msgs, "username": admin_auth.username if admin_auth else "admin"},
        )

    @router.post("/login")
    def admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
        # 限速:按来源IP,防在线爆破 admin 口令
        ip = request.client.host if request.client else "?"
        tkey = f"admin:{ip}"
        if login_throttle:
            wait = login_throttle.check(tkey)
            if wait > 0:
                return _flash_redirect("/admin/login",
                                       ("error", f"尝试过于频繁,请 {int(wait) + 1} 秒后再试"))
        if not admin_auth or not admin_auth.verify_login(username, password):
            if login_throttle:
                login_throttle.record_failure(tkey)
            return _flash_redirect("/admin/login", ("error", "用户名或密码错误"))
        if login_throttle:
            login_throttle.record_success(tkey)
        token = admin_auth.login()
        resp = RedirectResponse("/admin/", status_code=303)
        # 同一应用有两个入口:本机 HTTP :8442 与局域网 HTTPS :8443。
        # Secure 必须按**本次请求协议**设置;若仅因磁盘上存在 TLS 证书就设 True,
        # 本机 HTTP 登录成功后浏览器不会回传 cookie,表现为无限跳回登录页。
        secure = request.url.scheme == "https"
        resp.set_cookie(ADMIN_COOKIE, token, max_age=SESSION_TTL,
                        httponly=True, samesite="lax", secure=secure)
        return resp

    @router.post("/logout")
    def admin_logout(request: Request):
        if admin_auth:
            admin_auth.logout(request.cookies.get(ADMIN_COOKIE))
        resp = RedirectResponse("/admin/login", status_code=303)
        resp.delete_cookie(ADMIN_COOKIE)
        return resp

    @router.get("/account", response_class=HTMLResponse)
    def admin_account(request: Request):
        import json as _json
        from host.admin_auth import EMAIL_PRESETS
        s = admin_auth.smtp if admin_auth else {}
        return templates.TemplateResponse(
            request, "account.html",
            {
                "active": "account",
                "username": admin_auth.username if admin_auth else "admin",
                "email": admin_auth.email if admin_auth else "",
                "initialized": admin_auth.initialized if admin_auth else False,
                "smtp": s,
                "presets": EMAIL_PRESETS,
                "hints_json": _json.dumps({k: p["hint"] for k, p in EMAIL_PRESETS.items()},
                                          ensure_ascii=False),
                "messages": _pop_messages(request),
            },
        )

    @router.post("/account/email")
    def admin_set_email(request: Request, email: str = Form(...), code: str = Form("")):
        e = (email or "").strip()
        if "@" not in e or "." not in e.split("@")[-1]:
            return _flash_redirect("/admin/account", ("error", "邮箱格式不正确"))
        # 初始化完成后改邮箱需验证码(发到当前邮箱);初始化阶段(向导)邮箱可自由改。
        if admin_auth.initialized:
            if e == admin_auth.email:
                return _flash_redirect("/admin/account", ("error", "新邮箱与当前邮箱相同"))
            if not admin_auth.check_code(code, "change_email"):
                return _flash_redirect("/admin/account", ("error", "验证码错误或已过期,请点「发送验证码」重新获取"))
            admin_auth.set_email(e)
            return _flash_redirect("/admin/account", ("success", f"已更换为邮箱 {e}"))
        # 向导阶段:直接绑定,进入第二步(配置邮件发送)
        admin_auth.set_email(e)
        return _flash_redirect("/admin/account", ("success", f"已绑定邮箱 {e},请继续配置邮件发送"))

    @router.post("/account/smtp")
    def admin_set_smtp(request: Request,
                       provider: str = Form("qq"),
                       email: str = Form(""),
                       auth_code: str = Form(""),
                       host: str = Form(""), port: str = Form("587"),
                       use_ssl_custom: str = Form(""), use_tls_custom: str = Form(""),
                       init: str = Form("")):
        from host.admin_auth import EMAIL_PRESETS
        cur = admin_auth.smtp
        email = (email or "").strip()
        pw = auth_code if auth_code else cur.get("password", "")   # 留空=不改授权码
        if provider != "custom":
            p = EMAIL_PRESETS.get(provider, EMAIL_PRESETS["qq"])
            cfg = {"provider": provider, "host": p["host"], "port": p["port"],
                   "use_ssl": bool(p.get("ssl")), "use_tls": bool(p.get("tls")),
                   "user": email, "from": email, "password": pw}
        else:
            cfg = {"provider": "custom", "host": host.strip(), "port": int(port or 587),
                   "use_ssl": use_ssl_custom == "on", "use_tls": use_tls_custom == "on",
                   "user": email, "from": email, "password": pw}
        admin_auth.set_smtp(cfg)
        # 首次初始化第二步:保存后发测试邮件验证;成功才算"初始化完成"。
        if init == "1" and not admin_auth.initialized:
            if not admin_auth.email:
                return _flash_redirect("/admin/account", ("error", "请先返回上一步绑定邮箱"))
            ok, detail = admin_auth.send_test_email(admin_auth.email)
            if ok:
                admin_auth.set_initialized(True)
                return _flash_redirect("/admin/account",
                                       ("success", f"初始化完成!{detail}。此后可分别修改密码 / 邮箱 / 发送设置。"))
            return _flash_redirect("/admin/account",
                                   ("warning", f"邮件设置已保存,但{detail} 配置成功前仍可返回上一步修改邮箱。"))
        return _flash_redirect("/admin/account", ("success", "已保存邮件配置"))

    @router.post("/account/send_code")
    def admin_send_code(request: Request, purpose: str = Form("change_pw")):
        if not admin_auth.email:
            return _flash_redirect("/admin/account", ("error", "请先绑定邮箱"))
        purpose = purpose if purpose in ("change_pw", "change_email") else "change_pw"
        what = "邮箱" if purpose == "change_email" else "登录密码"
        code = admin_auth.gen_code(purpose)
        ok, detail = admin_auth.send_code_email(admin_auth.email, code, what)
        return _flash_redirect("/admin/account",
                               ("success" if ok else "warning", detail))

    @router.post("/account/password")
    def admin_change_password(request: Request,
                              code: str = Form(...),
                              new_password: str = Form(...),
                              confirm: str = Form(...)):
        if not admin_auth.email:
            return _flash_redirect("/admin/account", ("error", "请先绑定邮箱"))
        if len(new_password) < 6:
            return _flash_redirect("/admin/account", ("error", "新密码至少 6 位"))
        if new_password != confirm:
            return _flash_redirect("/admin/account", ("error", "两次输入的新密码不一致"))
        if not admin_auth.check_code(code, "change_pw"):
            return _flash_redirect("/admin/account", ("error", "验证码错误或已过期,请重新获取"))
        admin_auth.set_password(new_password)
        resp = _flash_redirect("/admin/login", ("success", "密码已修改,请用新密码重新登录"))
        resp.delete_cookie(ADMIN_COOKIE)
        return resp

    # ============================================================
    # 概览
    # ============================================================
    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        # 全段防御性:任何子系统抛错都 fall back 到 0/[],不让概览页 500。
        # 这样即使 LLM 代理未配置、CallStat 还没数据、auth_manager 异常,
        # 概览仍能正常展示。
        def _safe(fn, default):
            try:
                return fn()
            except Exception:
                return default

        all_auths = _safe(lambda: list(auth_manager._auths.values()), [])
        active_auths = [a for a in all_auths if _safe(a.is_valid, False)]
        revoked_auths = [a for a in all_auths if getattr(a, "revoked", False)]

        all_accounts = _safe(lambda: list(user_manager._accounts.values()), [])
        active_accounts = [a for a in all_accounts if str(getattr(a, "status", "")) == "active"]
        disabled_accounts = [a for a in all_accounts if str(getattr(a, "status", "")) == "disabled"]

        sessions = _safe(lambda: list(user_manager._sessions.values()), [])

        # ---- LLM 配置 + 统计(代理可能尚未配置;留空即可)----
        configs = _safe(llm_config_store.list_all, [])
        empty_totals = {
            "calls": 0, "success": 0, "failed": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "total_tokens": 0, "cost_usd": 0.0,
        }
        totals       = _safe(lambda: call_stats.totals("all"),        dict(empty_totals))
        totals_day   = _safe(lambda: call_stats.totals("today"),      dict(empty_totals))
        totals_month = _safe(lambda: call_stats.totals("this_month"), dict(empty_totals))

        by_model       = _safe(lambda: call_stats.by_model("all"),        [])
        by_model_day   = _safe(lambda: call_stats.by_model("today"),      [])
        by_model_month = _safe(lambda: call_stats.by_model("this_month"), [])

        by_user        = _safe(lambda: call_stats.by_user("all"),        [])
        by_user_day    = _safe(lambda: call_stats.by_user("today"),      [])
        by_user_month  = _safe(lambda: call_stats.by_user("this_month"), [])

        recent_days   = _safe(lambda: call_stats.recent_days(7),    [])
        recent_months = _safe(lambda: call_stats.recent_months(6),  [])

        unconfigured_users = [
            a.username for a in all_accounts
            if not getattr(a, "llm_config_id", None)
        ]

        stats = {
            "authorizations": len(all_auths),
            "active_authorizations": len(active_auths),
            "revoked_authorizations": len(revoked_auths),
            "accounts": len(all_accounts),
            "active_accounts": len(active_accounts),
            "disabled_accounts": len(disabled_accounts),
            "sessions": len(sessions),
            "llm_configs": len(configs),
            "llm_calls": totals.get("calls", 0),
            "llm_success": totals.get("success", 0),
            "llm_failed": totals.get("failed", 0),
            "llm_total_tokens": totals.get("total_tokens", 0),
            "llm_cost_usd": float(totals.get("cost_usd", 0.0) or 0.0),
            # 当日 / 当月简报(KPI 卡的 hint 上展示)
            "llm_calls_day":   totals_day.get("calls", 0),
            "llm_calls_month": totals_month.get("calls", 0),
            "llm_tokens_day":   totals_day.get("total_tokens", 0),
            "llm_tokens_month": totals_month.get("total_tokens", 0),
            "llm_cost_day":   float(totals_day.get("cost_usd", 0.0) or 0.0),
            "llm_cost_month": float(totals_month.get("cost_usd", 0.0) or 0.0),
        }

        system = {
            "version": "0.2.0",
            "hetorch_ready": False,
            "license_days_left": _safe(_peek_license_days, 0),
            "llm_configured": len(configs) > 0,
        }

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "active": "dashboard",
                "stats": stats,
                "system": system,
                "by_model": by_model,
                "by_model_day": by_model_day,
                "by_model_month": by_model_month,
                "by_user": by_user,
                "by_user_day": by_user_day,
                "by_user_month": by_user_month,
                "recent_days": recent_days,
                "recent_months": recent_months,
                "unconfigured_users": unconfigured_users,
                "messages": _pop_messages(request),
            },
        )

    router.include_router(build_users_router(
        templates=templates,
        auth_manager=auth_manager,
        user_manager=user_manager,
        llm_config_store=llm_config_store,
        flash_redirect=_flash_redirect,
        pop_messages=_pop_messages,
    ))
    router.include_router(build_llm_router(
        templates=templates,
        llm_config_store=llm_config_store,
        provider_manager=provider_manager,
        user_manager=user_manager,
        flash_redirect=_flash_redirect,
        pop_messages=_pop_messages,
    ))

    router.include_router(build_data_sources_router(
        templates=templates,
        data_source_store=data_source_store,
        connector_registry=connector_registry,
        flash_redirect=_flash_redirect,
        pop_messages=_pop_messages,
    ))

    router.include_router(build_data_permissions_router(
        templates=templates,
        service=permission_admin,
        data_access_store=data_access_store,
        user_manager=user_manager,
        flash_redirect=_flash_redirect,
        pop_messages=_pop_messages,
    ))
    router.include_router(build_data_usage_router(
        templates=templates,
        data_source_store=data_source_store,
        data_access_store=data_access_store,
        user_manager=user_manager,
        pop_messages=_pop_messages,
    ))

    router.include_router(build_sessions_router(
        templates=templates,
        user_manager=user_manager,
        flash_redirect=_flash_redirect,
        pop_messages=_pop_messages,
    ))
    router.include_router(build_ops_router(templates=templates, pop_messages=_pop_messages))

    # ============================================================
    # 站内信(只读通知 + 留痕)—— 从授权 / LLM 用量 / 服务状态派生,读取时同步
    # ============================================================
    def _sync_admin_notices() -> None:
        from datetime import date
        seen = _notice_store.seen_keys()

        # 1) 用户授权失效(被吊销 / 客户端初始化失败)→ 注意
        try:
            for a in list(auth_manager._auths.values()):
                revoked = bool(getattr(a, "revoked", False))
                init_failed = getattr(a, "sdk_init_failed_at", None) is not None
                if not (revoked or init_failed):
                    continue
                key = f"auth_invalid:{a.auth_id}:{'r' if revoked else 'f'}"
                if key in seen:
                    continue
                why = "已被吊销" if revoked else "客户端初始化失败、已被标记失效"
                _notice_store.add(
                    key=key, level="warning",
                    title=f"用户授权失效 · {a.subject}",
                    summary=(f"用户「{a.subject}」的密态授权{why},该用户当前无法进行密态计算。"
                             f"请到「用户」页核实,必要时重新签发 / 导入授权文件。"),
                    created_at=_iso(getattr(a, "imported_at", None)))
        except Exception:
            pass

        # 2) LLM 调用今日有失败(每日一条)→ 注意
        try:
            nf = int((call_stats.totals("today") or {}).get("failed", 0) or 0)
            if nf > 0:
                key = f"llmfail:{date.today().isoformat()}"
                if key not in seen:
                    _notice_store.add(
                        key=key, level="warning",
                        title="LLM 调用今日有失败",
                        summary=(f"今天已有 {nf} 次 LLM 调用失败。常见原因:API key 失效 / 额度不足 / "
                                 f"网络不通 / 模型不可用。请到「LLM 配置」页核查对应配置。"))
        except Exception:
            pass

        # 3) 服务被守护重启 / 守护未运行 → 提示 / 注意
        try:
            from host import service_manager as sm
            snap = sm.status_snapshot()
            for svc in snap.get("services", []):
                n = int(svc.get("restarts", 0) or 0)
                if n <= 0:
                    continue
                key = f"restart:{svc.get('key')}:{n}"
                if key in seen:
                    continue
                _notice_store.add(
                    key=key, level="info",
                    title=f"服务被守护重启 · {svc.get('label') or svc.get('key')}",
                    summary=(f"{svc.get('label') or '服务'}(端口 {svc.get('port')})累计被守护重启 {n} 次,"
                             f"最近一次 {svc.get('last_restart') or '—'}。若频繁重启,请到「运维」页查看日志排查。"),
                    created_at=str(svc.get("last_restart") or ""))
            if not snap.get("supervisor", {}).get("running", False):
                key = f"sup_down:{date.today().isoformat()}"
                if key not in seen:
                    _notice_store.add(
                        key=key, level="warning",
                        title="守护进程未运行",
                        summary=("主机守护(supervisor)当前未运行,服务崩溃后将无法自动重启。"
                                 "请到「运维」页启动守护,或启用开机自启 + 守护。"))
        except Exception:
            pass

    @router.get("/notices.json")
    def admin_notices_json(request: Request):
        _sync_admin_notices()   # 读取即同步:近实时(前端轮询)+ 补发(停机期间的信号)
        return JSONResponse({
            "items": [n.to_dict() for n in _notice_store.list()],
            "unread": _notice_store.unread_count(),
        })

    @router.post("/notices/read")
    def admin_notices_read(request: Request):
        _notice_store.mark_all_read()
        return JSONResponse({"ok": True, "unread": 0})

    return router


# ---------------------------------------------------------------------------
# Flash messages — 基于 URL query 跨 POST→redirect→GET 传递
# ---------------------------------------------------------------------------


def _flash_redirect(url: str, *messages: tuple[str, str]) -> RedirectResponse:
    """构造一个带 flash 消息的 303 redirect。"""
    if messages:
        from urllib.parse import quote
        parts = [f"_flash={quote(f'{c}|{m}', safe='')}" for c, m in messages]
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{'&'.join(parts)}"
    return RedirectResponse(url, status_code=303)


def _pop_messages(request: Request) -> list[tuple[str, str]]:
    """从当前请求 URL 的 _flash 参数读取消息列表。"""
    out: list[tuple[str, str]] = []
    from urllib.parse import parse_qs

    for val in parse_qs(request.url.query).get("_flash", []):
        if "|" in val:
            cat, msg = val.split("|", 1)
            out.append((cat, msg))
    return out


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------




def _peek_license_days() -> int:
    """简化:固定返回(SDK 不暴露 API)。"""
    return 213
