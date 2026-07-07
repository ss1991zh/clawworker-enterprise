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

import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from host.llm_configs import (
    CallStatStore,
    FALLBACK_MODELS,
    LLMConfigStore,
    PROVIDER_PRESETS,
    ProviderManager,
    discover_models,
)
from host.notices import NoticeStore

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


def _mask_token(token: str, keep: int = 6) -> str:
    if not token:
        return "—"
    if len(token) <= keep * 2:
        return "*" * len(token)
    return f"{token[:keep]}...{token[-keep:]}"


def build_admin_router(
    *,
    auth_manager,
    user_manager,
    dispatcher,
    llm_config_store: LLMConfigStore,
    provider_manager: ProviderManager,
    call_stats: CallStatStore,
    admin_auth=None,
) -> APIRouter:
    """构造 admin 路由 router。"""
    router = APIRouter(prefix="/admin", tags=["admin"])

    # ============================================================
    # 登录 / 退出 / 账户(密码 + 绑定邮箱 + 邮箱验证码改密)
    # ============================================================
    from host.admin_auth import COOKIE as ADMIN_COOKIE, SESSION_TTL

    @router.get("/login", response_class=HTMLResponse)
    def admin_login_form(request: Request):
        # 已登录直接进首页
        if admin_auth and admin_auth.valid(request.cookies.get(ADMIN_COOKIE)):
            return RedirectResponse("/admin/", status_code=303)
        return templates.TemplateResponse(
            request, "admin_login.html",
            {"messages": _pop_messages(request), "username": admin_auth.username if admin_auth else "admin"},
        )

    @router.post("/login")
    def admin_login(request: Request, username: str = Form(...), password: str = Form(...)):
        if not admin_auth or not admin_auth.verify_login(username, password):
            return _flash_redirect("/admin/login", ("error", "用户名或密码错误"))
        token = admin_auth.login()
        resp = RedirectResponse("/admin/", status_code=303)
        resp.set_cookie(ADMIN_COOKIE, token, max_age=SESSION_TTL,
                        httponly=True, samesite="lax")
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

    # ============================================================
    # 用户(合并:证书 + 账户 → 1 个实体)
    # ============================================================

    @router.get("/users", response_class=HTMLResponse)
    def user_list(request: Request):
        """每行 = (证书 + 账户 + LLM 配置)。证书生命周期跟随账户。"""
        configs = llm_config_store.list_all()
        cfg_index = {c.id: c for c in configs}
        # 防御性:每次进页面也再做一次清理(双保险)
        auth_manager.cleanup_unbound(set(user_manager._accounts.keys()))

        users = []
        for u in user_manager._accounts.values():
            auth = (
                auth_manager._auths.get(u.username)
                if hasattr(auth_manager, "_auths")
                else None
            )
            cfg = cfg_index.get(u.llm_config_id) if u.llm_config_id else None
            users.append({
                "username": u.username,
                "auth_id": u.auth_id,
                "status": u.status,
                "created_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "imported_at": auth.imported_at.strftime("%Y-%m-%d %H:%M:%S") if auth else "—",
                "auth_valid": auth.is_valid() if auth else False,
                "revoked": auth.revoked if auth else False,
                "llm_config_id": u.llm_config_id or "",
                "llm_config_name": cfg.name if cfg else "",
                "llm_model_name": cfg.model_name if cfg else "",
            })
        return templates.TemplateResponse(
            request, "users.html",
            {
                "active": "users",
                "users": users,
                "llm_configs": configs,
                "messages": _pop_messages(request),
            },
        )

    @router.post("/users")
    async def user_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        cert_file: UploadFile = File(...),
        llm_config_id: str = Form(""),
    ):
        """合并创建:同时导入证书 + 建账户(原子操作,失败回滚)。"""
        contents = await cert_file.read()
        if not contents:
            return _flash_redirect("/admin/users",
                                   ("error", "证书文件为空,请重新选择"))
        with tempfile.NamedTemporaryFile(delete=False, suffix=".auth") as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)

        # 验 llm_config_id(空 = 稍后补选)
        cfg_id: Optional[str] = llm_config_id.strip() or None
        if cfg_id and not llm_config_store.get(cfg_id):
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
            return _flash_redirect("/admin/users",
                                   ("error", f"LLM 配置 id={cfg_id} 不存在"))

        result: tuple[str, str] | None = None
        try:
            auth_manager.import_authorization(username=username, source=tmp_path)
            try:
                user_manager.create_account(
                    username=username,
                    password=password,
                    llm_config_id=cfg_id,
                )
            except Exception:
                auth_manager.delete(username)
                raise
            tail = ""
            if cfg_id:
                cfg = llm_config_store.get(cfg_id)
                tail = f" · 已绑定「{cfg.name}」" if cfg else ""
            else:
                tail = " · LLM 配置未选(请稍后补选)"
            result = ("success", f"已创建用户「{username}」· 证书 + 账户绑定完成{tail}")
        except FileNotFoundError as e:
            result = ("error", f"文件读取失败:{e}")
        except ValueError as e:
            result = ("error", str(e))
        except Exception as e:
            result = ("error", f"创建失败:{e}")
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        return _flash_redirect("/admin/users", result) if result else \
            RedirectResponse("/admin/users", status_code=303)

    @router.get("/users/{username}/edit", response_class=HTMLResponse)
    def user_edit_form(request: Request, username: str):
        acct = user_manager._accounts.get(username)
        if not acct:
            return _flash_redirect("/admin/users",
                                   ("error", f"用户 {username} 不存在"))
        configs = llm_config_store.list_all()
        cfg = llm_config_store.get(acct.llm_config_id) if acct.llm_config_id else None
        return templates.TemplateResponse(
            request, "user_edit.html",
            {
                "active": "users",
                "user": {
                    "username": username,
                    "status": acct.status,
                    "auth_id": acct.auth_id,
                    "created_at": acct.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "llm_config_id": acct.llm_config_id or "",
                    "llm_config_name": cfg.name if cfg else "",
                },
                "llm_configs": configs,
                "messages": _pop_messages(request),
            },
        )

    @router.post("/users/{username}/password")
    def user_update_password(request: Request, username: str, password: str = Form(...)):
        try:
            user_manager.update_password(username, password)
            return _flash_redirect(
                "/admin/users",
                ("success", f"已更新「{username}」的密码;该用户所有现有 session 已注销"),
            )
        except ValueError as e:
            return _flash_redirect("/admin/users", ("error", str(e)))

    @router.post("/users/{username}/llm_config")
    def user_set_llm_config(
        request: Request,
        username: str,
        llm_config_id: str = Form(""),
    ):
        cfg_id: Optional[str] = llm_config_id.strip() or None
        if cfg_id and not llm_config_store.get(cfg_id):
            return _flash_redirect("/admin/users",
                                   ("error", f"LLM 配置 id={cfg_id} 不存在"))
        try:
            user_manager.set_llm_config(username, cfg_id)
        except ValueError as e:
            return _flash_redirect("/admin/users", ("error", str(e)))
        msg = (
            f"已为用户「{username}」绑定 LLM 配置「{llm_config_store.get(cfg_id).name}」"
            if cfg_id else f"已清空用户「{username}」的 LLM 配置(稍后补选)"
        )
        return _flash_redirect("/admin/users", ("success", msg))

    @router.post("/users/{username}/disable")
    def user_disable(request: Request, username: str):
        user_manager.disable(username)
        return _flash_redirect("/admin/users", ("success", f"用户「{username}」已禁用"))

    @router.post("/users/{username}/enable")
    def user_enable(request: Request, username: str):
        user_manager.enable(username)
        return _flash_redirect("/admin/users", ("success", f"用户「{username}」已启用"))

    @router.post("/users/{username}/delete")
    def user_delete(request: Request, username: str):
        user_manager.delete_account(username)
        auth_manager.delete(username)
        return _flash_redirect(
            "/admin/users",
            ("success", f"已删除用户「{username}」· 证书已释放,可重新分配"),
        )

    # 向后兼容
    @router.get("/authorizations", response_class=HTMLResponse)
    def auth_list_compat(request: Request):
        return RedirectResponse("/admin/users", status_code=301)

    @router.get("/accounts", response_class=HTMLResponse)
    def account_list_compat(request: Request):
        return RedirectResponse("/admin/users", status_code=301)

    # ============================================================
    # LLM 配置(多配置 CRUD,统计放概览)
    # ============================================================

    @router.get("/llm", response_class=HTMLResponse)
    def llm_view(request: Request):
        configs_raw = llm_config_store.list_all()
        configs = []
        for c in configs_raw:
            preset = PROVIDER_PRESETS.get(c.provider_type, {})
            configs.append({
                "id": c.id,
                "name": c.name,
                "provider_type": c.provider_type,
                "provider_label": preset.get("label", c.provider_type),
                "model_name": c.model_name,
                "base_url": c.base_url or preset.get("base_url", "—"),
                "api_key_masked": c.masked_key(),
                "enabled": c.enabled,
                "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                # 是否就绪:Provider 缓存能 init(防御性,避免一次失败 init 卡住整个页面)
                "ready": _safe_ready(provider_manager, c.id),
            })

        # 供前端 JS:provider 切换时自动填默认 base_url + 默认模型列表
        provider_options = [
            {"id": pid, "label": p["label"], "base_url": p["base_url"], "kind": p["kind"]}
            for pid, p in PROVIDER_PRESETS.items()
        ]
        fallback_models_json = {pid: FALLBACK_MODELS.get(pid, []) for pid in PROVIDER_PRESETS}

        return templates.TemplateResponse(
            request,
            "llm.html",
            {
                "active": "llm",
                "configs": configs,
                "provider_options": provider_options,
                "fallback_models_json": fallback_models_json,
                "messages": _pop_messages(request),
            },
        )

    @router.post("/llm")
    async def llm_create(
        request: Request,
        name: str = Form(...),
        provider_type: str = Form(...),
        model_name: str = Form(...),
        api_key: str = Form(""),
        base_url: str = Form(""),
    ):
        try:
            cfg = llm_config_store.create(
                name=name.strip(),
                provider_type=provider_type.strip(),
                model_name=model_name.strip(),
                api_key=api_key.strip(),
                base_url=base_url.strip(),
            )
            provider_manager.invalidate(cfg.id)
        except ValueError as e:
            return _flash_redirect("/admin/llm", ("error", str(e)))
        return _flash_redirect("/admin/llm",
                               ("success", f"已创建 LLM 配置「{cfg.name}」(id={cfg.id})"))

    @router.get("/llm/{config_id}/edit", response_class=HTMLResponse)
    def llm_edit_form(request: Request, config_id: str):
        c = llm_config_store.get(config_id)
        if not c:
            return _flash_redirect("/admin/llm", ("error", "配置不存在"))
        preset = PROVIDER_PRESETS.get(c.provider_type, {})
        return templates.TemplateResponse(
            request, "llm_edit.html",
            {
                "active": "llm",
                "cfg": {
                    "id": c.id,
                    "name": c.name,
                    "provider_type": c.provider_type,
                    "provider_label": preset.get("label", c.provider_type),
                    "model_name": c.model_name,
                    "base_url": c.base_url or preset.get("base_url", "—"),
                    "api_key_masked": c.masked_key(),
                    "enabled": c.enabled,
                    "created_at": c.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                },
                "messages": _pop_messages(request),
            },
        )

    @router.post("/llm/{config_id}/update")
    async def llm_update(
        request: Request,
        config_id: str,
        name: str = Form(...),
    ):
        # 只允许改"配置名"。厂商 / 模型 / API Key / Base URL 一旦保存即锁定 ——
        # 它们决定了「概览」里按配置/模型聚合的调用统计,中途改会让历史统计口径错乱;
        # 如需换模型,请新建一份配置(旧统计仍归属旧配置)。
        if not name.strip():
            return _flash_redirect(f"/admin/llm/{config_id}/edit", ("error", "配置名不能为空"))
        try:
            cfg = llm_config_store.update(config_id, name=name.strip())
        except ValueError as e:
            return _flash_redirect(f"/admin/llm/{config_id}/edit", ("error", str(e)))
        return _flash_redirect("/admin/llm",
                               ("success", f"已更新配置名为「{cfg.name}」"))

    @router.post("/llm/{config_id}/toggle")
    def llm_toggle(request: Request, config_id: str):
        cfg = llm_config_store.get(config_id)
        if not cfg:
            return _flash_redirect("/admin/llm", ("error", "配置不存在"))
        new_enabled = not cfg.enabled
        llm_config_store.update(config_id, enabled=new_enabled)
        provider_manager.invalidate(config_id)
        verb = "启用" if new_enabled else "禁用"
        return _flash_redirect("/admin/llm",
                               ("success", f"已{verb}「{cfg.name}」"))

    @router.post("/llm/{config_id}/delete")
    def llm_delete(request: Request, config_id: str):
        cfg = llm_config_store.get(config_id)
        if not cfg:
            return _flash_redirect("/admin/llm", ("error", "配置不存在"))
        # 把所有指向它的用户解绑
        for acct in user_manager._accounts.values():
            if acct.llm_config_id == config_id:
                acct.llm_config_id = None
        llm_config_store.delete(config_id)
        provider_manager.invalidate(config_id)
        return _flash_redirect(
            "/admin/llm",
            ("success", f"已删除「{cfg.name}」· 引用该配置的用户已自动解绑(请重新选)"),
        )

    @router.post("/llm/discover")
    async def llm_discover(
        request: Request,
        provider_type: str = Form(...),
        api_key: str = Form(""),
        base_url: str = Form(""),
        config_id: str = Form(""),
    ):
        """AJAX:根据 api_key 拉取可用模型列表。

        修改已有配置时,前端 api_key 是空(不显示明文)。这时如果带了
        config_id,就用 store 里存的真实 key 去探测。
        """
        effective_key = api_key.strip()
        used_stored = False
        if not effective_key and config_id:
            cfg = llm_config_store.get(config_id.strip())
            if cfg and cfg.api_key:
                effective_key = cfg.api_key
                used_stored = True
                # base_url 也补一下(用户没改的话用 store 里的)
                if not base_url.strip():
                    base_url = cfg.base_url or ""

        models, source = discover_models(
            provider_type.strip(),
            effective_key,
            base_url.strip(),
        )
        return JSONResponse({
            "models": models,
            "source": source,
            "count": len(models),
            "used_stored_key": used_stored,
        })

    # ============================================================
    # 会话
    # ============================================================

    def _safe_records(fn, default):
        try:
            return fn()
        except Exception:
            return default

    def _safe_back(url: str) -> str:
        """只允许跳回 /admin/ 下的内部地址,避免开放重定向。"""
        return url if (url or "").startswith("/admin/") else "/admin/sessions"

    @router.get("/sessions", response_class=HTMLResponse)
    def session_list(request: Request):
        from host import client_records as cr
        users = _safe_records(cr.list_users, [])
        sessions = [
            {
                "username": s.username,
                "token": s.token,
                "token_masked": _mask_token(s.token),
                "expires_at": s.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
            }
            for s in user_manager._sessions.values()
        ]
        return templates.TemplateResponse(
            request, "sessions.html",
            {"active": "sessions", "users": users, "sessions": sessions,
             "messages": _pop_messages(request)},
        )

    @router.get("/sessions/user/{username}", response_class=HTMLResponse)
    def user_records(request: Request, username: str):
        from host import client_records as cr
        show_hidden = request.query_params.get("show_hidden") == "1"
        chats = [s for s in _safe_records(cr.list_sessions, []) if s["username"] == username]
        tasks = [t for t in _safe_records(cr.list_tasks, []) if t["username"] == username]
        hidden_n = sum(1 for r in chats if r["hidden"]) + sum(1 for r in tasks if r["hidden"])
        if not show_hidden:
            chats = [r for r in chats if not r["hidden"]]
            tasks = [r for r in tasks if not r["hidden"]]
        # 普通会话 / 定时任务 两个 tab 分开
        tab = "scheduled" if request.query_params.get("tab") == "scheduled" else "normal"
        normal_chats = [c for c in chats if c["kind"] != "scheduled"]
        # 任务 → 其运行会话 的映射(不论会话是否被隐藏,任务的运行记录都能看)
        sess_by_task = {s["task_id"]: s["id"]
                        for s in _safe_records(cr.list_sessions, [])
                        if s.get("task_id")}

        sel = None       # 选中会话(普通 tab)/ 选中任务的运行会话(定时 tab)
        sel_task = None  # 选中的任务(定时 tab)
        if tab == "scheduled":
            # 左列表 = 任务(与任务列表 1:1);选中任务后读它的运行会话(可能还没有消息)
            tid = request.query_params.get("t", "")
            if tid and any(t["id"] == tid for t in tasks):
                sel_task = next(t for t in tasks if t["id"] == tid)
            elif tasks:
                sel_task = tasks[0]
            if sel_task:
                sess_id = sess_by_task.get(sel_task["id"])
                sel = cr.get_session(sess_id) if sess_id else None
        else:
            sid = request.query_params.get("s", "")
            if sid and any(c["id"] == sid for c in normal_chats):
                sel = cr.get_session(sid)
            elif normal_chats:
                sel = cr.get_session(normal_chats[0]["id"])
        return templates.TemplateResponse(
            request, "user_records.html",
            {"active": "sessions", "username": username, "tab": tab,
             "normal_chats": normal_chats, "tasks": tasks,
             "sel": sel, "sel_task": sel_task,
             "normal_n": len(normal_chats),
             "show_hidden": show_hidden, "hidden_n": hidden_n,
             "messages": _pop_messages(request)},
        )

    @router.post("/sessions/{token}/revoke")
    def session_revoke(request: Request, token: str):
        user_manager.logout(token)
        return _flash_redirect("/admin/sessions", ("success", "登录会话已注销"))

    # 清除/恢复记录显示 —— 仅 admin 视图,不影响用户任何数据
    @router.post("/records/{kind}/{rid}/hide")
    def record_hide(request: Request, kind: str, rid: str, back: str = Form("")):
        from host import client_records as cr
        if kind not in ("session", "task"):
            return _flash_redirect("/admin/sessions", ("error", "未知记录类型"))
        cr.hide(f"{kind}:{rid}")
        return _flash_redirect(_safe_back(back),
                               ("success", "已从列表隐藏(仅本页显示,用户侧不受影响)"))

    @router.post("/records/{kind}/{rid}/unhide")
    def record_unhide(request: Request, kind: str, rid: str, back: str = Form("")):
        from host import client_records as cr
        if kind not in ("session", "task"):
            return _flash_redirect("/admin/sessions", ("error", "未知记录类型"))
        cr.unhide(f"{kind}:{rid}")
        return _flash_redirect(_safe_back(back), ("success", "已恢复显示"))

    # ============================================================
    # 运维 / 服务(开机自启 + 健康监控 + 崩溃自愈)
    # ============================================================

    @router.get("/ops", response_class=HTMLResponse)
    def ops_view(request: Request):
        from host import service_manager as sm

        def _safe(fn, default):
            try:
                return fn()
            except Exception:
                return default

        snap = _safe(sm.status_snapshot, {
            "platform": sm.current_platform(),
            "supervisor": {"running": False, "pid": None},
            "autostart": {"installed": False, "kind": "unknown", "detail": ""},
            "services": [],
        })
        return templates.TemplateResponse(
            request, "ops.html",
            {
                "active": "ops",
                "snap": snap,
                "supervisor_py": str(sm.SUPERVISOR_PY),
                "log_path": str(sm.SUP_LOG),
                "messages": _pop_messages(request),
            },
        )

    @router.get("/ops/status.json")
    def ops_status_json(request: Request):
        from host import service_manager as sm
        try:
            return JSONResponse(sm.status_snapshot())
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=500)

    @router.post("/ops/autostart/enable")
    def ops_autostart_enable(request: Request):
        from host import service_manager as sm
        try:
            msg = sm.install_autostart()
            sm.ensure_supervisor_running()
            return _flash_redirect("/admin/ops", ("success", f"已启用开机自启 + 守护 · {msg}"))
        except Exception as e:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"启用失败:{e}"))

    @router.post("/ops/autostart/disable")
    def ops_autostart_disable(request: Request):
        from host import service_manager as sm
        try:
            msg = sm.uninstall_autostart()
            sm.stop_supervisor(kill_children=False)  # 停守护但保留服务,admin 不掉线
            return _flash_redirect("/admin/ops",
                                   ("success", f"已停用开机自启 · {msg} · 守护已停(服务仍在运行)"))
        except Exception as e:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"停用失败:{e}"))

    @router.post("/ops/supervisor/start")
    def ops_supervisor_start(request: Request):
        from host import service_manager as sm
        try:
            started = sm.ensure_supervisor_running()
            msg = "守护已启动" if started else "守护已在运行"
            return _flash_redirect("/admin/ops", ("success", msg))
        except Exception as e:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"启动守护失败:{e}"))

    @router.post("/ops/supervisor/stop")
    def ops_supervisor_stop(request: Request):
        from host import service_manager as sm
        try:
            msg = sm.stop_supervisor(kill_children=False)
            return _flash_redirect("/admin/ops", ("success", f"{msg}(子服务保留)"))
        except Exception as e:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"停止守护失败:{e}"))

    @router.post("/ops/restart/{which}")
    def ops_restart(request: Request, which: str):
        from host import service_manager as sm
        try:
            msg = sm.restart_service(which)
            return _flash_redirect("/admin/ops", ("success", msg))
        except Exception as e:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"重启失败:{e}"))

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


def _default_base_url(provider_type: Optional[str]) -> str:
    preset = PROVIDER_PRESETS.get(provider_type or "")
    return preset["base_url"] if preset else "—"


def _safe_ready(provider_manager: ProviderManager, config_id: str) -> bool:
    """provider 初始化抛错时不阻塞 LLM 页面渲染。"""
    try:
        return provider_manager.for_config(config_id) is not None
    except Exception:
        return False


def _peek_license_days() -> int:
    """简化:固定返回(SDK 不暴露 API)。"""
    return 213
