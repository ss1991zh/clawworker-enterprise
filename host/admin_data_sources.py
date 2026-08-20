"""管理后台企业数据源路由。"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse


def build_data_sources_router(*, templates, data_source_store, connector_registry,
                              flash_redirect: Callable, pop_messages: Callable) -> APIRouter:
    router = APIRouter(prefix="/data-sources")

    @router.get("", response_class=HTMLResponse)
    def data_source_list(request: Request):
        sources = []
        if data_source_store:
            for source in data_source_store.list_all():
                item = source.__dict__.copy()
                item["catalog"] = data_source_store.catalog_summary(source.id)
                sources.append(item)
        return templates.TemplateResponse(
            request, "data_sources.html",
            {"active": "data_sources", "sources": sources,
             "messages": pop_messages(request)},
        )

    @router.post("")
    def data_source_create(
        name: str = Form(...), engine: str = Form(...), host: str = Form(...),
        port: int = Form(...), database_name: str = Form(...),
        username: str = Form(...), password: str = Form(...),
        ssl_mode: str = Form("prefer"), ca_path: str = Form(""),
        connect_timeout_seconds: int = Form(8), query_timeout_seconds: int = Form(60),
        max_rows: int = Form(10000), max_result_mb: int = Form(50),
        max_concurrent_queries: int = Form(1), max_queries_per_minute: int = Form(20),
    ):
        if not data_source_store:
            return flash_redirect("/admin/data-sources", ("error", "数据源模块未初始化"))
        try:
            source = data_source_store.create(
                name=name, engine=engine, host=host, port=port,
                database_name=database_name, username=username, password=password,
                ssl_mode=ssl_mode, ca_path=ca_path,
                connect_timeout_seconds=connect_timeout_seconds,
                query_timeout_seconds=query_timeout_seconds, max_rows=max_rows,
                max_result_bytes=max_result_mb * 1024 * 1024,
                max_concurrent_queries=max_concurrent_queries,
                max_queries_per_minute=max_queries_per_minute,
            )
        except ValueError as exc:
            return flash_redirect("/admin/data-sources", ("error", str(exc)))
        return flash_redirect(
            "/admin/data-sources",
            ("success", f"已创建数据源「{source.name}」；请先测试连接，再同步结构"),
        )

    @router.get("/{source_id}/edit", response_class=HTMLResponse)
    def data_source_edit_form(request: Request, source_id: str):
        source = data_source_store.get(source_id) if data_source_store else None
        if not source:
            return flash_redirect("/admin/data-sources", ("error", "数据源不存在"))
        return templates.TemplateResponse(
            request, "data_source_edit.html",
            {"active": "data_sources", "source": source,
             "catalog": data_source_store.catalog_summary(source.id),
             "messages": pop_messages(request)},
        )

    @router.post("/{source_id}/update")
    def data_source_update(
        source_id: str, name: str = Form(...), engine: str = Form(...),
        host: str = Form(...), port: int = Form(...), database_name: str = Form(...),
        username: str = Form(...), password: str = Form(""),
        ssl_mode: str = Form("prefer"), ca_path: str = Form(""),
        connect_timeout_seconds: int = Form(8), query_timeout_seconds: int = Form(60),
        max_rows: int = Form(10000), max_result_mb: int = Form(50),
        max_concurrent_queries: int = Form(1), max_queries_per_minute: int = Form(20),
    ):
        try:
            source = data_source_store.update(
                source_id, name=name, engine=engine, host=host, port=port,
                database_name=database_name, username=username, password=password,
                ssl_mode=ssl_mode, ca_path=ca_path,
                connect_timeout_seconds=connect_timeout_seconds,
                query_timeout_seconds=query_timeout_seconds, max_rows=max_rows,
                max_result_bytes=max_result_mb * 1024 * 1024,
                max_concurrent_queries=max_concurrent_queries,
                max_queries_per_minute=max_queries_per_minute,
            )
        except ValueError as exc:
            return flash_redirect(f"/admin/data-sources/{source_id}/edit", ("error", str(exc)))
        return flash_redirect("/admin/data-sources", ("success", f"已更新数据源「{source.name}」"))

    @router.post("/{source_id}/test")
    def data_source_test(source_id: str):
        source = data_source_store.get(source_id) if data_source_store else None
        if not source or not connector_registry:
            return flash_redirect("/admin/data-sources", ("error", "数据源不存在或模块未初始化"))
        try:
            result = connector_registry.test(source, data_source_store.get_password(source_id))
        except Exception as exc:  # noqa: BLE001
            from host.db_connectors import safe_error
            result = type("Result", (), {"ok": False, "message": safe_error(exc),
                                         "server_version": ""})()
        detail = result.message + (f" · 版本 {result.server_version}" if result.server_version else "")
        data_source_store.record_test(source_id, result.ok, detail)
        return flash_redirect(
            "/admin/data-sources",
            (("success" if result.ok else "error"), f"「{source.name}」：{detail}"),
        )

    @router.post("/{source_id}/sync")
    def data_source_sync(source_id: str):
        source = data_source_store.get(source_id) if data_source_store else None
        if not source or not connector_registry:
            return flash_redirect("/admin/data-sources", ("error", "数据源不存在或模块未初始化"))
        try:
            columns = connector_registry.inspect_catalog(
                source, data_source_store.get_password(source_id),
            )
            count = data_source_store.replace_catalog(source_id, columns)
            summary = data_source_store.catalog_summary(source_id)
        except Exception as exc:  # noqa: BLE001
            from host.db_connectors import safe_error
            return flash_redirect(
                "/admin/data-sources", ("error", f"「{source.name}」同步失败：{safe_error(exc)}"),
            )
        return flash_redirect(
            "/admin/data-sources",
            ("success", f"「{source.name}」已同步 {summary['schemas']} 个架构、"
                        f"{summary['tables']} 张表/视图、{count} 个字段"),
        )

    @router.post("/{source_id}/toggle")
    def data_source_toggle(source_id: str):
        source = data_source_store.get(source_id) if data_source_store else None
        if not source:
            return flash_redirect("/admin/data-sources", ("error", "数据源不存在"))
        updated = data_source_store.set_enabled(source_id, not source.enabled)
        return flash_redirect(
            "/admin/data-sources",
            ("success", f"已{'启用' if updated.enabled else '停用'}「{updated.name}」"),
        )

    @router.post("/{source_id}/delete")
    def data_source_delete(source_id: str):
        source = data_source_store.get(source_id) if data_source_store else None
        if not source:
            return flash_redirect("/admin/data-sources", ("error", "数据源不存在"))
        data_source_store.delete(source_id)
        return flash_redirect(
            "/admin/data-sources",
            ("success", f"已删除数据源「{source.name}」及其本地结构目录；远程数据库未受影响"),
        )

    return router
