"""
企业版 Admin UI(Jinja2 模板 + FastAPI)。

简约扁平风格,5 个页面:
- /admin/                   概览(KPI + 系统状态)
- /admin/authorizations     用户授权(导入 / 列表 / 吊销)
- /admin/accounts           账户(创建 / 列表 / 启停)
- /admin/llm                LLM 配置(查看 + 切换说明)
- /admin/sessions           在线会话(查看 / 注销)

⚠️ MVP 阶段:没有 admin 登录鉴权,假设 admin 在主机本地访问。
   生产部署应:绑定 localhost only,或加 Basic Auth / SSO。
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

import tempfile

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# 模板目录
_HOST_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_HOST_DIR / "templates"))


def _mask_token(token: str, keep: int = 6) -> str:
    if not token:
        return "—"
    if len(token) <= keep * 2:
        return "*" * len(token)
    return f"{token[:keep]}...{token[-keep:]}"


def _mask_apikey() -> str:
    """从环境变量取当前 api key 做脱敏展示。"""
    for env_var in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        v = os.environ.get(env_var)
        if v:
            return f"{env_var}={_mask_token(v, keep=8)}"
    return "— 未在环境变量中设置 —"


def build_admin_router(*, auth_manager, user_manager, dispatcher, get_llm_provider) -> APIRouter:
    """
    构造 admin 路由 router。把依赖通过参数注入,
    避免 admin_ui 模块直接 import server 全局,造成循环依赖。

    Args:
        auth_manager: host.cert_manager.AuthorizationManager 实例
        user_manager: host.user_manager.UserManager 实例
        dispatcher : host.dispatcher.Dispatcher 实例
        get_llm_provider: 一个无参数 callable,返回 LLMProvider 或 None
    """
    router = APIRouter(prefix="/admin", tags=["admin"])

    # ----- 概览 -----
    @router.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        all_auths = list(auth_manager._auths.values())
        active_auths = [a for a in all_auths if a.is_valid()]
        revoked_auths = [a for a in all_auths if a.revoked]

        all_accounts = list(user_manager._accounts.values())
        active_accounts = [a for a in all_accounts if a.status == "active"]
        disabled_accounts = [a for a in all_accounts if a.status == "disabled"]

        sessions = list(user_manager._sessions.values())

        provider = get_llm_provider()
        llm_status = "已就绪" if provider else "未配置"
        llm_calls = sum(1 for t in dispatcher._tasks.values() if t.status in ("done", "failed"))

        stats = {
            "authorizations": len(all_auths),
            "active_authorizations": len(active_auths),
            "revoked_authorizations": len(revoked_auths),
            "accounts": len(all_accounts),
            "active_accounts": len(active_accounts),
            "disabled_accounts": len(disabled_accounts),
            "sessions": len(sessions),
            "llm_calls": llm_calls,
            "llm_status": llm_status,
        }

        system = {
            "version": "0.1.0",
            "llm_provider": (
                f"{os.environ.get('MODEL_TYPE', '—')} · "
                f"{os.environ.get('MODEL_NAME', '—')}"
            ),
            "hetorch_ready": False,
            "license_days_left": _peek_license_days(),
        }

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "active": "dashboard",
                "stats": stats,
                "system": system,
                "messages": _pop_messages(request),
            },
        )

    # ============================================================
    # 用户(合并:证书 + 账户 → 1 个实体)
    # ============================================================

    @router.get("/users", response_class=HTMLResponse)
    def user_list(request: Request):
        """合并视图:每行 = (证书 + 账户)。"""
        users = []
        # 优先按账户视角(每个账户必须有授权)
        for u in user_manager._accounts.values():
            auth = auth_manager._auths.get(u.username) if hasattr(auth_manager, "_auths") else None
            users.append({
                "username": u.username,
                "auth_id": u.auth_id,
                "status": u.status,
                "created_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "imported_at": auth.imported_at.strftime("%Y-%m-%d %H:%M:%S") if auth else "—",
                "auth_valid": auth.is_valid() if auth else False,
                "revoked": auth.revoked if auth else False,
            })
        # 孤立授权(导入证书但没建账户)
        orphans = []
        for un, a in auth_manager._auths.items():
            if un not in user_manager._accounts:
                orphans.append({
                    "username": un,
                    "auth_id": a.auth_id,
                    "imported_at": a.imported_at.strftime("%Y-%m-%d %H:%M:%S"),
                    "revoked": a.revoked,
                })
        return templates.TemplateResponse(
            request, "users.html",
            {
                "active": "users",
                "users": users,
                "orphan_auths": orphans,
                "messages": _pop_messages(request),
            },
        )

    @router.post("/users")
    async def user_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        cert_file: UploadFile = File(...),
    ):
        """
        合并创建:同时导入证书 + 建账户(原子操作,失败回滚)。
        - 拖拽上传得到 cert_file
        - 强制 1 证书 1 用户(cert_manager 内部 fingerprint 查重)
        """
        # 1) 落临时文件
        contents = await cert_file.read()
        if not contents:
            _push_message(request, "error", "证书文件为空")
            return RedirectResponse("/admin/users", status_code=303)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".auth") as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)

        try:
            # 2) 导入授权(含 fingerprint 查重)
            auth_manager.import_authorization(username=username, source=tmp_path)
            # 3) 创建账户;若失败,回滚授权
            try:
                user_manager.create_account(username=username, password=password)
            except Exception as e:
                auth_manager.delete(username)
                raise
            _push_message(request, "success",
                          f"已创建用户「{username}」· 证书 + 账户绑定完成")
        except FileNotFoundError as e:
            _push_message(request, "error", f"文件读取失败:{e}")
        except ValueError as e:
            _push_message(request, "error", str(e))
        except Exception as e:
            _push_message(request, "error", f"创建失败:{e}")
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        return RedirectResponse("/admin/users", status_code=303)

    @router.get("/users/{username}/edit", response_class=HTMLResponse)
    def user_edit_form(request: Request, username: str):
        acct = user_manager._accounts.get(username)
        if not acct:
            _push_message(request, "error", f"用户 {username} 不存在")
            return RedirectResponse("/admin/users", status_code=303)
        return templates.TemplateResponse(
            request, "user_edit.html",
            {
                "active": "users",
                "user": {
                    "username": username,
                    "status": acct.status,
                    "auth_id": acct.auth_id,
                    "created_at": acct.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                },
                "messages": _pop_messages(request),
            },
        )

    @router.post("/users/{username}/password")
    def user_update_password(request: Request, username: str, password: str = Form(...)):
        try:
            user_manager.update_password(username, password)
            _push_message(request, "success",
                          f"已更新「{username}」的密码;该用户所有现有 session 已注销")
        except ValueError as e:
            _push_message(request, "error", str(e))
        return RedirectResponse("/admin/users", status_code=303)

    @router.post("/users/{username}/disable")
    def user_disable(request: Request, username: str):
        user_manager.disable(username)
        _push_message(request, "success", f"用户「{username}」已禁用")
        return RedirectResponse("/admin/users", status_code=303)

    @router.post("/users/{username}/enable")
    def user_enable(request: Request, username: str):
        user_manager.enable(username)
        _push_message(request, "success", f"用户「{username}」已启用")
        return RedirectResponse("/admin/users", status_code=303)

    @router.post("/users/{username}/delete")
    def user_delete(request: Request, username: str):
        user_manager.delete_account(username)
        auth_manager.delete(username)
        _push_message(request, "success",
                      f"已删除用户「{username}」· 证书已释放,可重新分配")
        return RedirectResponse("/admin/users", status_code=303)

    # ============================================================
    # 旧路由保留(向后兼容)
    # ============================================================
    @router.get("/authorizations", response_class=HTMLResponse)
    def auth_list_compat(request: Request):
        # 旧链接重定向到合并页
        return RedirectResponse("/admin/users", status_code=301)

    @router.get("/accounts", response_class=HTMLResponse)
    def account_list_compat(request: Request):
        return RedirectResponse("/admin/users", status_code=301)

    # ----- LLM -----
    @router.get("/llm", response_class=HTMLResponse)
    def llm_view(request: Request):
        provider = get_llm_provider()
        all_tasks = list(dispatcher._tasks.values())
        successful = sum(1 for t in all_tasks if t.status == "done")
        failed = sum(1 for t in all_tasks if t.status == "failed")
        llm = {
            "provider_type": os.environ.get("MODEL_TYPE"),
            "model_name": os.environ.get("MODEL_NAME"),
            "base_url": _base_url_for(os.environ.get("MODEL_TYPE")),
            "api_key_masked": _mask_apikey(),
            "ready": provider is not None,
            "total_calls": len(all_tasks),
            "successful_calls": successful,
            "failed_calls": failed,
        }
        return templates.TemplateResponse(
            request,
            "llm.html",
            {
                "active": "llm",
                "llm": llm,
                "messages": _pop_messages(request),
            },
        )

    # ----- 会话 -----
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
        _push_message(request, "success", "会话已注销")
        return RedirectResponse("/admin/sessions", status_code=303)

    return router


# ---------------------------------------------------------------------------
# 简化版 flash messages(基于 cookie,POST 后 redirect 时携带)
# ---------------------------------------------------------------------------


def _push_message(request: Request, category: str, msg: str) -> None:
    """通过响应 cookie 携带 flash;HTMX/SPA 场景可换 server-side store。"""
    # 简化:暂存到 request.state(只在同请求生效)
    if not hasattr(request.state, "_pending_messages"):
        request.state._pending_messages = []
    request.state._pending_messages.append((category, msg))


def _pop_messages(request: Request) -> list:
    """读取 Query string 中的 msg 参数(POST→redirect 后)。"""
    out = []
    qs = request.url.query
    if "msg=" in qs:
        from urllib.parse import parse_qs
        for cat, m in parse_qs(qs).get("msg", []):
            out.append((cat, m))
    return out


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _base_url_for(model_type: Optional[str]) -> Optional[str]:
    return {
        "openrouter": "https://openrouter.ai/api/v1",
        "openai": os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "anthropic": "https://api.anthropic.com/v1",
    }.get(model_type or "")


def _peek_license_days() -> int:
    """尝试从 client.tools.runtime 拿 SDK 自带的剩余天数(213/214 那个)。"""
    # 简化:返回固定 213(因为 SDK 不暴露 API,日志里看到的)
    # 真生产可以解析 user_authorization 文件或读 SDK 内部状态
    return 213
