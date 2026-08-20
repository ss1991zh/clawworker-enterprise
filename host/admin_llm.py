"""管理后台 LLM 配置路由。"""
from __future__ import annotations

from typing import Callable

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse

from host.llm_configs import FALLBACK_MODELS, PROVIDER_PRESETS, discover_models


def _safe_ready(provider_manager, config_id: str) -> bool:
    try:
        return provider_manager.for_config(config_id) is not None
    except Exception:  # noqa: BLE001
        return False


def build_llm_router(*, templates, llm_config_store, provider_manager, user_manager,
                     flash_redirect: Callable, pop_messages: Callable) -> APIRouter:
    router = APIRouter(prefix="/llm")

    @router.get("", response_class=HTMLResponse)
    def llm_view(request: Request):
        configs = []
        for config in llm_config_store.list_all():
            preset = PROVIDER_PRESETS.get(config.provider_type, {})
            configs.append({
                "id": config.id, "name": config.name,
                "provider_type": config.provider_type,
                "provider_label": preset.get("label", config.provider_type),
                "model_name": config.model_name,
                "base_url": config.base_url or preset.get("base_url", "—"),
                "api_key_masked": config.masked_key(), "enabled": config.enabled,
                "created_at": config.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "ready": _safe_ready(provider_manager, config.id),
            })
        options = [
            {"id": provider_id, "label": preset["label"],
             "base_url": preset["base_url"], "kind": preset["kind"]}
            for provider_id, preset in PROVIDER_PRESETS.items()
        ]
        return templates.TemplateResponse(
            request, "llm.html",
            {"active": "llm", "configs": configs, "provider_options": options,
             "fallback_models_json": {
                 provider_id: FALLBACK_MODELS.get(provider_id, [])
                 for provider_id in PROVIDER_PRESETS
             }, "messages": pop_messages(request)},
        )

    @router.post("")
    async def llm_create(name: str = Form(...), provider_type: str = Form(...),
                         model_name: str = Form(...), api_key: str = Form(""),
                         base_url: str = Form("")):
        try:
            config = llm_config_store.create(
                name=name.strip(), provider_type=provider_type.strip(),
                model_name=model_name.strip(), api_key=api_key.strip(),
                base_url=base_url.strip(),
            )
            provider_manager.invalidate(config.id)
        except ValueError as exc:
            return flash_redirect("/admin/llm", ("error", str(exc)))
        return flash_redirect(
            "/admin/llm", ("success", f"已创建 LLM 配置「{config.name}」(id={config.id})"),
        )

    @router.get("/{config_id}/edit", response_class=HTMLResponse)
    def llm_edit_form(request: Request, config_id: str):
        config = llm_config_store.get(config_id)
        if not config:
            return flash_redirect("/admin/llm", ("error", "配置不存在"))
        preset = PROVIDER_PRESETS.get(config.provider_type, {})
        return templates.TemplateResponse(
            request, "llm_edit.html",
            {"active": "llm", "cfg": {
                "id": config.id, "name": config.name,
                "provider_type": config.provider_type,
                "provider_label": preset.get("label", config.provider_type),
                "model_name": config.model_name,
                "base_url": config.base_url or preset.get("base_url", "—"),
                "api_key_masked": config.masked_key(), "enabled": config.enabled,
                "created_at": config.created_at.strftime("%Y-%m-%d %H:%M:%S"),
            }, "messages": pop_messages(request)},
        )

    @router.post("/{config_id}/update")
    async def llm_update(config_id: str, name: str = Form(...)):
        if not name.strip():
            return flash_redirect(f"/admin/llm/{config_id}/edit", ("error", "配置名不能为空"))
        try:
            config = llm_config_store.update(config_id, name=name.strip())
        except ValueError as exc:
            return flash_redirect(f"/admin/llm/{config_id}/edit", ("error", str(exc)))
        return flash_redirect("/admin/llm", ("success", f"已更新配置名为「{config.name}」"))

    @router.post("/{config_id}/toggle")
    def llm_toggle(config_id: str):
        config = llm_config_store.get(config_id)
        if not config:
            return flash_redirect("/admin/llm", ("error", "配置不存在"))
        enabled = not config.enabled
        llm_config_store.update(config_id, enabled=enabled)
        provider_manager.invalidate(config_id)
        return flash_redirect(
            "/admin/llm", ("success", f"已{'启用' if enabled else '禁用'}「{config.name}」"),
        )

    @router.post("/{config_id}/delete")
    def llm_delete(config_id: str):
        config = llm_config_store.get(config_id)
        if not config:
            return flash_redirect("/admin/llm", ("error", "配置不存在"))
        for account in user_manager._accounts.values():
            if account.llm_config_id == config_id:
                account.llm_config_id = None
        llm_config_store.delete(config_id)
        provider_manager.invalidate(config_id)
        return flash_redirect(
            "/admin/llm",
            ("success", f"已删除「{config.name}」· 引用该配置的用户已自动解绑(请重新选)"),
        )

    @router.post("/discover")
    async def llm_discover(provider_type: str = Form(...), api_key: str = Form(""),
                           base_url: str = Form(""), config_id: str = Form("")):
        effective_key, used_stored = api_key.strip(), False
        if not effective_key and config_id:
            config = llm_config_store.get(config_id.strip())
            if config and config.api_key:
                effective_key, used_stored = config.api_key, True
                if not base_url.strip():
                    base_url = config.base_url or ""
        models, source = discover_models(
            provider_type.strip(), effective_key, base_url.strip(),
        )
        return JSONResponse({"models": models, "source": source, "count": len(models),
                             "used_stored_key": used_stored})

    return router
