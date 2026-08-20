"""管理端用户、账户与授权证书路由。"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse


def build_users_router(
    *,
    templates,
    auth_manager,
    user_manager,
    llm_config_store,
    flash_redirect,
    pop_messages,
) -> APIRouter:
    router = APIRouter()

    @router.get("/users", response_class=HTMLResponse)
    def user_list(request: Request):
        configs = llm_config_store.list_all()
        cfg_index = {c.id: c for c in configs}
        auth_manager.cleanup_unbound(set(user_manager._accounts.keys()))
        users = []
        for user in user_manager._accounts.values():
            auth = auth_manager._auths.get(user.username) if hasattr(auth_manager, "_auths") else None
            cfg = cfg_index.get(user.llm_config_id) if user.llm_config_id else None
            users.append({
                "username": user.username,
                "auth_id": user.auth_id,
                "status": user.status,
                "created_at": user.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "imported_at": auth.imported_at.strftime("%Y-%m-%d %H:%M:%S") if auth else "—",
                "auth_valid": auth.is_valid() if auth else False,
                "revoked": auth.revoked if auth else False,
                "llm_config_id": user.llm_config_id or "",
                "llm_config_name": cfg.name if cfg else "",
                "llm_model_name": cfg.model_name if cfg else "",
            })
        return templates.TemplateResponse(
            request, "users.html",
            {"active": "users", "users": users, "llm_configs": configs,
             "messages": pop_messages(request)},
        )

    @router.post("/users")
    async def user_create(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
        cert_file: UploadFile = File(...),
        llm_config_id: str = Form(""),
    ):
        contents = await cert_file.read()
        if not contents:
            return flash_redirect("/admin/users", ("error", "证书文件为空,请重新选择"))
        with tempfile.NamedTemporaryFile(delete=False, suffix=".auth") as tmp:
            tmp.write(contents)
            tmp_path = Path(tmp.name)
        cfg_id: Optional[str] = llm_config_id.strip() or None
        if cfg_id and not llm_config_store.get(cfg_id):
            tmp_path.unlink(missing_ok=True)
            return flash_redirect("/admin/users", ("error", f"LLM 配置 id={cfg_id} 不存在"))
        result: tuple[str, str] | None = None
        try:
            auth_manager.import_authorization(username=username, source=tmp_path)
            try:
                user_manager.create_account(username=username, password=password, llm_config_id=cfg_id)
            except Exception:
                auth_manager.delete(username)
                raise
            cfg = llm_config_store.get(cfg_id) if cfg_id else None
            tail = f" · 已绑定「{cfg.name}」" if cfg else " · LLM 配置未选(请稍后补选)"
            result = ("success", f"已创建用户「{username}」· 证书 + 账户绑定完成{tail}")
        except FileNotFoundError as exc:
            result = ("error", f"文件读取失败:{exc}")
        except ValueError as exc:
            result = ("error", str(exc))
        except Exception as exc:
            result = ("error", f"创建失败:{exc}")
        finally:
            tmp_path.unlink(missing_ok=True)
        return flash_redirect("/admin/users", result) if result else RedirectResponse("/admin/users", status_code=303)

    @router.get("/users/{username}/edit", response_class=HTMLResponse)
    def user_edit_form(request: Request, username: str):
        acct = user_manager._accounts.get(username)
        if not acct:
            return flash_redirect("/admin/users", ("error", f"用户 {username} 不存在"))
        configs = llm_config_store.list_all()
        cfg = llm_config_store.get(acct.llm_config_id) if acct.llm_config_id else None
        return templates.TemplateResponse(
            request, "user_edit.html",
            {"active": "users", "user": {
                "username": username, "status": acct.status, "auth_id": acct.auth_id,
                "created_at": acct.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "llm_config_id": acct.llm_config_id or "", "llm_config_name": cfg.name if cfg else "",
            }, "llm_configs": configs, "messages": pop_messages(request)},
        )

    @router.post("/users/{username}/password")
    def user_update_password(request: Request, username: str, password: str = Form(...)):
        try:
            user_manager.update_password(username, password)
            return flash_redirect("/admin/users", ("success", f"已更新「{username}」的密码;该用户所有现有 session 已注销"))
        except ValueError as exc:
            return flash_redirect("/admin/users", ("error", str(exc)))

    @router.post("/users/{username}/llm_config")
    def user_set_llm_config(request: Request, username: str, llm_config_id: str = Form("")):
        cfg_id: Optional[str] = llm_config_id.strip() or None
        if cfg_id and not llm_config_store.get(cfg_id):
            return flash_redirect("/admin/users", ("error", f"LLM 配置 id={cfg_id} 不存在"))
        try:
            user_manager.set_llm_config(username, cfg_id)
        except ValueError as exc:
            return flash_redirect("/admin/users", ("error", str(exc)))
        msg = (f"已为用户「{username}」绑定 LLM 配置「{llm_config_store.get(cfg_id).name}」"
               if cfg_id else f"已清空用户「{username}」的 LLM 配置(稍后补选)")
        return flash_redirect("/admin/users", ("success", msg))

    @router.post("/users/{username}/disable")
    def user_disable(request: Request, username: str):
        user_manager.disable(username)
        return flash_redirect("/admin/users", ("success", f"用户「{username}」已禁用"))

    @router.post("/users/{username}/enable")
    def user_enable(request: Request, username: str):
        user_manager.enable(username)
        return flash_redirect("/admin/users", ("success", f"用户「{username}」已启用"))

    @router.post("/users/{username}/delete")
    def user_delete(request: Request, username: str):
        user_manager.delete_account(username)
        auth_manager.delete(username)
        return flash_redirect("/admin/users", ("success", f"已删除用户「{username}」· 证书已释放,可重新分配"))

    @router.get("/authorizations", response_class=HTMLResponse)
    def auth_list_compat(request: Request):
        return RedirectResponse("/admin/users", status_code=301)

    @router.get("/accounts", response_class=HTMLResponse)
    def account_list_compat(request: Request):
        return RedirectResponse("/admin/users", status_code=301)

    return router
