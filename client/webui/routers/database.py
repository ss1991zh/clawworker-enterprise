"""企业数据库 HTTP 路由。接口路径保持与 1.6.x 完全兼容。"""
from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request

from client.webui.services.database import (
    DatabaseAnalysisService,
    DatabaseResultEncryptionError,
)


def build_database_router(
    *,
    host_api: Callable[..., Any],
    service: DatabaseAnalysisService,
    is_logged_in: Callable[[], bool],
    need_login: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/data", tags=["enterprise-database"])

    @router.get("/sources")
    def data_sources():
        return host_api("GET", "/data/sources")

    @router.get("/sources/{source_id}/catalog")
    def data_catalog(source_id: str):
        return host_api("GET", f"/data/sources/{quote(source_id, safe='')}/catalog")

    @router.post("/query/preview")
    async def query_preview(request: Request):
        if not is_logged_in():
            return need_login()
        return host_api("POST", "/data/query/preview", json_body=await request.json())

    @router.post("/query/plan")
    async def query_plan(request: Request):
        if not is_logged_in():
            return need_login()
        return host_api(
            "POST", "/data/query/plan", json_body=await request.json(), timeout=120.0,
        )

    @router.post("/query/execute")
    async def query_execute(request: Request):
        if not is_logged_in():
            return need_login()
        return host_api("POST", "/data/query/tasks", json_body=await request.json())

    @router.get("/query/tasks/{task_id}")
    def query_task(task_id: str):
        return host_api("GET", f"/data/query/tasks/{quote(task_id, safe='')}")

    @router.delete("/query/tasks/{task_id}")
    def query_cancel(task_id: str):
        return host_api("DELETE", f"/data/query/tasks/{quote(task_id, safe='')}")

    @router.post("/query/tasks/{task_id}/result")
    def query_result(task_id: str):
        result = host_api(
            "POST", f"/data/query/tasks/{quote(task_id, safe='')}/result", timeout=300.0,
        )
        if not isinstance(result, dict):
            raise HTTPException(502, "管理端返回的查询结果格式无效")
        try:
            return service.encrypt_result(
                result,
                data_source_id=str(result.get("data_source_id") or "db"),
            )
        except DatabaseResultEncryptionError as exc:
            raise HTTPException(400, f"查询成功但自动加密失败：{exc}") from exc

    return router
