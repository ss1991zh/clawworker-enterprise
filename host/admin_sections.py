"""管理后台可独立测试的轻量路由分区。"""
from __future__ import annotations

import json
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse


def _flash_redirect(url: str, *messages: tuple[str, str]) -> RedirectResponse:
    if messages:
        from urllib.parse import quote
        query = "&".join(f"_flash={quote(f'{category}|{message}', safe='')}"
                         for category, message in messages)
        url = f"{url}{'&' if '?' in url else '?'}{query}"
    return RedirectResponse(url, status_code=303)


def build_data_usage_router(*, templates, data_source_store, data_access_store,
                            user_manager,
                            pop_messages: Callable[[Request], list]) -> APIRouter:
    router = APIRouter(prefix="/data-usage")

    @router.get("", response_class=HTMLResponse)
    def data_usage_list(request: Request, username: str = "", data_source_id: str = "",
                        operation: str = "", status: str = ""):
        sources = data_source_store.list_all() if data_source_store else []
        source_names = {source.id: source.name for source in sources}
        audits = []
        if data_access_store:
            for row in data_access_store.list_audits(
                500, username=username.strip(), data_source_id=data_source_id.strip(),
                operation=operation.strip(), status=status.strip(),
            ):
                item = dict(row)
                item["source_name"] = source_names.get(
                    item["data_source_id"], item["data_source_id"] or "全部授权目录",
                )
                try:
                    item["tables"] = json.loads(item.pop("tables_json"))
                    item["columns"] = json.loads(item.pop("columns_json"))
                except (ValueError, TypeError):
                    item["tables"], item["columns"] = [], []
                audits.append(item)
        return templates.TemplateResponse(
            request, "data_usage.html",
            {"active": "data_usage", "audits": audits, "sources": sources,
             "users": sorted(user_manager._accounts.keys()),
             "summary": data_access_store.audit_summary() if data_access_store else {},
             "filters": {"username": username, "data_source_id": data_source_id,
                         "operation": operation, "status": status},
             "messages": pop_messages(request)},
        )

    return router


def build_ops_router(*, templates, pop_messages: Callable[[Request], list]) -> APIRouter:
    router = APIRouter(prefix="/ops")

    @router.get("", response_class=HTMLResponse)
    def ops_view(request: Request):
        from host import service_manager as sm

        try:
            snapshot = sm.status_snapshot()
        except Exception:  # noqa: BLE001
            snapshot = {
                "platform": sm.current_platform(),
                "supervisor": {"running": False, "pid": None},
                "autostart": {"installed": False, "kind": "unknown", "detail": ""},
                "services": [],
            }
        return templates.TemplateResponse(
            request, "ops.html",
            {"active": "ops", "snap": snapshot, "supervisor_py": str(sm.SUPERVISOR_PY),
             "log_path": str(sm.SUP_LOG), "messages": pop_messages(request)},
        )

    @router.get("/status.json")
    def ops_status_json():
        from host import service_manager as sm
        try:
            return JSONResponse(sm.status_snapshot())
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    @router.post("/autostart/enable")
    def ops_autostart_enable():
        from host import service_manager as sm
        try:
            message = sm.install_autostart()
            sm.ensure_supervisor_running()
            return _flash_redirect("/admin/ops", ("success", f"已启用开机自启 + 守护 · {message}"))
        except Exception as exc:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"启用失败:{exc}"))

    @router.post("/autostart/disable")
    def ops_autostart_disable():
        from host import service_manager as sm
        try:
            message = sm.uninstall_autostart()
            sm.stop_supervisor(kill_children=False)
            return _flash_redirect(
                "/admin/ops", ("success", f"已停用开机自启 · {message} · 守护已停(服务仍在运行)"),
            )
        except Exception as exc:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"停用失败:{exc}"))

    @router.post("/supervisor/start")
    def ops_supervisor_start():
        from host import service_manager as sm
        try:
            started = sm.ensure_supervisor_running()
            return _flash_redirect("/admin/ops", ("success", "守护已启动" if started else "守护已在运行"))
        except Exception as exc:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"启动守护失败:{exc}"))

    @router.post("/supervisor/stop")
    def ops_supervisor_stop():
        from host import service_manager as sm
        try:
            message = sm.stop_supervisor(kill_children=False)
            return _flash_redirect("/admin/ops", ("success", f"{message}(子服务保留)"))
        except Exception as exc:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"停止守护失败:{exc}"))

    @router.post("/restart/{which}")
    def ops_restart(which: str):
        from host import service_manager as sm
        try:
            return _flash_redirect("/admin/ops", ("success", sm.restart_service(which)))
        except Exception as exc:  # noqa: BLE001
            return _flash_redirect("/admin/ops", ("error", f"重启失败:{exc}"))

    return router
