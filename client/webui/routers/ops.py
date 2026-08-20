"""用户端自启动与守护进程运维路由。"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException


def build_ops_router(
    *, is_logged_in: Callable[[], bool], need_login: Callable[[], Any],
) -> APIRouter:
    from client.webui import client_ops

    router = APIRouter(prefix="/api/ops", tags=["client-operations"])

    @router.get("/status")
    def status():
        if not is_logged_in():
            return need_login()
        try:
            return client_ops.status_snapshot()
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"{type(exc).__name__}: {exc}") from exc

    @router.post("/autostart/enable")
    def autostart_enable():
        if not is_logged_in():
            return need_login()
        try:
            message = client_ops.install_autostart()
            client_ops.ensure_supervisor_running()
            return {"ok": True, "msg": message}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"启用失败:{exc}") from exc

    @router.post("/autostart/disable")
    def autostart_disable():
        if not is_logged_in():
            return need_login()
        try:
            message = client_ops.uninstall_autostart()
            client_ops.stop_supervisor()
            return {"ok": True, "msg": message}
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, f"停用失败:{exc}") from exc

    @router.post("/supervisor/start")
    def supervisor_start():
        if not is_logged_in():
            return need_login()
        started = client_ops.ensure_supervisor_running()
        return {"ok": True, "msg": "守护已启动" if started else "守护已在运行"}

    @router.post("/supervisor/stop")
    def supervisor_stop():
        if not is_logged_in():
            return need_login()
        return {"ok": True, "msg": client_ops.stop_supervisor()}

    return router
