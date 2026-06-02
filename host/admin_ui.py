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

# 模板目录
_HOST_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HOST_DIR / "templates"))


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
) -> APIRouter:
    """构造 admin 路由 router。"""
    router = APIRouter(prefix="/admin", tags=["admin"])

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
        totals = _safe(
            call_stats.totals,
            {"calls": 0, "success": 0, "failed": 0,
             "prompt_tokens": 0, "completion_tokens": 0,
             "total_tokens": 0, "cost_usd": 0.0},
        )
        by_model = _safe(call_stats.by_model, [])
        by_user = _safe(call_stats.by_user, [])

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
                "by_user": by_user,
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

    @router.post("/llm/{config_id}/update")
    async def llm_update(
        request: Request,
        config_id: str,
        name: str = Form(""),
        provider_type: str = Form(""),
        model_name: str = Form(""),
        api_key: str = Form(""),
        base_url: str = Form(""),
        enabled: str = Form(""),  # "on" / ""
    ):
        try:
            cfg = llm_config_store.update(
                config_id,
                name=name.strip() or None,
                provider_type=provider_type.strip() or None,
                model_name=model_name.strip() or None,
                api_key=api_key,  # 不 strip,空字符串 = 不改
                base_url=base_url.strip() if base_url else "",
                enabled=(enabled == "on") if enabled in ("on", "off") else None,
            )
            provider_manager.invalidate(cfg.id)
        except ValueError as e:
            return _flash_redirect("/admin/llm", ("error", str(e)))
        return _flash_redirect("/admin/llm",
                               ("success", f"已更新 LLM 配置「{cfg.name}」"))

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
    ):
        """AJAX:根据 api_key 拉取可用模型列表(失败 → fallback)。"""
        models, source = discover_models(
            provider_type.strip(),
            api_key.strip(),
            base_url.strip(),
        )
        return JSONResponse({
            "models": models,
            "source": source,
            "count": len(models),
        })

    # ============================================================
    # 会话
    # ============================================================

    @router.get("/sessions", response_class=HTMLResponse)
    def session_list(request: Request):
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
            request,
            "sessions.html",
            {
                "active": "sessions",
                "sessions": sessions,
                "messages": _pop_messages(request),
            },
        )

    @router.post("/sessions/{token}/revoke")
    def session_revoke(request: Request, token: str):
        user_manager.logout(token)
        return _flash_redirect("/admin/sessions", ("success", "会话已注销"))

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
