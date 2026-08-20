"""当前用户与本地客户端设置路由。"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request


def build_settings_router(
    *,
    session_snapshot: Callable[[], dict],
    config_snapshot: Callable[[], dict],
    update_config: Callable[[dict], dict],
    is_logged_in: Callable[[], bool],
    need_login: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["client-settings"])

    @router.get("/me")
    def current_user():
        if not is_logged_in():
            return need_login()
        state = session_snapshot()
        return {
            "username": state.get("username", ""),
            "host_url": state.get("host_url", ""),
            "expires_at": state.get("expires_at", ""),
        }

    @router.get("/config")
    def get_config():
        if not is_logged_in():
            return need_login()
        return config_snapshot()

    @router.post("/config")
    async def set_config(request: Request):
        if not is_logged_in():
            return need_login()
        try:
            return update_config(await request.json())
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    return router
