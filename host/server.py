"""
主机 HTTP 服务(FastAPI)。

提供两类端点:
- /auth/login           账号密码登录,返回 session token
- /llm/chat             调用 LLM 代理(需 session token,按用户绑定的 LLM 配置路由)

启动:
    uvicorn host.server:app --host 0.0.0.0 --port 8443
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from host.admin_ui import build_admin_router
from host.cert_manager import AuthorizationManager
from host.dispatcher import Dispatcher
from host.llm_configs import (
    CallStatStore,
    LLMConfigStore,
    ProviderManager,
    estimate_cost,
)
from host.llm_proxy import LLMProvider, make_provider
from host.user_manager import UserManager


# ---------------------------------------------------------------------------
# 全局组件(MVP 单进程)
# ---------------------------------------------------------------------------

auth_manager = AuthorizationManager()
user_manager = UserManager(auth_manager)
# 向后兼容别名
cert_manager = auth_manager
dispatcher = Dispatcher()

llm_config_store = LLMConfigStore()
provider_manager = ProviderManager(llm_config_store)
call_stats = CallStatStore()


def _bootstrap_legacy_env_config() -> None:
    """
    主机首次启动且没有任何配置时,把旧的 MODEL_TYPE / *_API_KEY 环境变量
    自动落成一条 LLMConfig,避免老部署升级后界面里"空空如也"。
    已有配置则跳过。
    """
    if llm_config_store.list_all():
        return
    model_type = os.environ.get("MODEL_TYPE", "")
    if not model_type:
        return
    key_var = {
        "anthropic": "ANTHROPIC_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "openai": "OPENAI_API_KEY",
    }.get(model_type)
    api_key = os.environ.get(key_var or "", "") if key_var else ""
    if not api_key:
        return
    default_model = {
        "anthropic": "claude-sonnet-4-5",
        "openrouter": "deepseek/deepseek-v4-pro",
        "openai": "gpt-4o",
    }.get(model_type, "")
    model_name = os.environ.get("MODEL_NAME", default_model)
    try:
        llm_config_store.create(
            name=f"{model_type}:{model_name}",
            provider_type=model_type,
            model_name=model_name,
            api_key=api_key,
            base_url=os.environ.get("OPENAI_BASE_URL", "") if model_type == "openai" else "",
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="agent-system host", version="0.2.0")

# Admin UI 路由 + 静态文件
from pathlib import Path as _P

_HOST_DIR = _P(__file__).resolve().parent
app.mount("/admin/static", StaticFiles(directory=str(_HOST_DIR / "static")), name="admin_static")
app.include_router(
    build_admin_router(
        auth_manager=auth_manager,
        user_manager=user_manager,
        dispatcher=dispatcher,
        llm_config_store=llm_config_store,
        provider_manager=provider_manager,
        call_stats=call_stats,
    )
)


@app.on_event("startup")
def startup():
    _bootstrap_legacy_env_config()
    # 1 张证书 ↔ 1 个账户:清理任何"无对应账户"的证书,以及磁盘上残留的孤儿 .auth
    released = auth_manager.cleanup_unbound(set(user_manager._accounts.keys()))
    if released:
        print(f"[startup] 释放 {len(released)} 张无主证书: {released}")


# ----- 鉴权依赖 -----


def get_current_session(authorization: str = Header(...)):
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, "无效的 Authorization header")
    token = authorization.removeprefix("Bearer ").strip()
    sess = user_manager.verify_session(token)
    if not sess:
        raise HTTPException(401, "session 无效或已过期")
    return sess


# ----- 登录 -----


class LoginRequest(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(req: LoginRequest):
    try:
        sess = user_manager.login(username=req.username, password=req.password)
    except PermissionError as e:
        raise HTTPException(401, str(e))
    return {"token": sess.token, "expires_at": sess.expires_at.isoformat()}


@app.get("/auth/user_authorization")
def fetch_user_authorization(sess=Depends(get_current_session)):
    """
    返回当前 session 用户绑定的 user_authorization 文件。

    用途:客户端登录后通过此端点把"主机端备份的证书"拉回本地 keystore,
    用户不再需要手动找证书文件 —— 证书的颁发与回收都由 admin 完成,
    客户端只是一份"沙盒副本"。
    """
    auth = auth_manager.get_by_username(sess.username)
    if not auth or not auth.is_valid():
        raise HTTPException(404, "用户授权不存在或已失效")
    if not auth.file_path.exists():
        raise HTTPException(404, "授权文件已被删除")
    return FileResponse(
        auth.file_path,
        media_type="application/octet-stream",
        filename=f"{sess.username}.user_authorization",
    )


# ----- 用户授权导入(admin)-----


class ImportAuthorizationRequest(BaseModel):
    username: str
    path: str  # user_authorization 文件路径(本机)


@app.post("/admin/authorization/import")
def import_authorization(req: ImportAuthorizationRequest):
    auth = auth_manager.import_authorization(
        username=req.username, source=Path(req.path)
    )
    return {
        "auth_id": auth.auth_id,
        "subject": auth.subject,
        "imported_at": auth.imported_at.isoformat(),
        "revoked": auth.revoked,
    }


class CreateAccountRequest(BaseModel):
    username: str
    password: str
    auth_id: Optional[str] = None  # 不传则按 username 自动查找已导入的授权
    llm_config_id: Optional[str] = None


@app.post("/admin/account/create")
def create_account(req: CreateAccountRequest):
    try:
        acct = user_manager.create_account(
            username=req.username,
            password=req.password,
            auth_id=req.auth_id,
            llm_config_id=req.llm_config_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {
        "username": acct.username,
        "status": acct.status,
        "llm_config_id": acct.llm_config_id,
    }


# ----- LLM 代理 -----


class ChatRequest(BaseModel):
    system: str
    user: str


def _resolve_user_provider(username: str):
    """共用:按用户绑定的 LLM 配置返回 (provider, cfg) 或 raise 503。"""
    acct = user_manager._accounts.get(username)
    if not acct or not acct.llm_config_id:
        raise HTTPException(
            503,
            "用户未绑定 LLM 配置;请到 Admin → 用户 → 修改 → 选择 LLM 配置",
        )
    cfg = llm_config_store.get(acct.llm_config_id)
    if not cfg or not cfg.enabled:
        raise HTTPException(
            503,
            f"绑定的 LLM 配置不可用(id={acct.llm_config_id});请重新选择",
        )
    provider = provider_manager.for_config(cfg.id)
    if provider is None:
        raise HTTPException(
            503,
            f"LLM provider 初始化失败(配置「{cfg.name}」);请检查 api_key / 网络",
        )
    return provider, cfg


@app.post("/llm/freechat")
def freechat(req: ChatRequest, sess=Depends(get_current_session)):
    """
    自由对话(纯文本输入 / 纯文本输出),不强制 parse computation_plan。
    用于客户端"会话还没绑定密文文件"的场景:用户只是想跟 AI 聊天。
    """
    provider, cfg = _resolve_user_provider(sess.username)
    try:
        text = provider.raw_chat(system=req.system, user=req.user)
    except Exception as e:
        call_stats.record(
            config=cfg, username=sess.username,
            prompt_tokens=0, completion_tokens=0,
            success=False, cost_usd=0.0,
        )
        raise HTTPException(500, f"LLM 调用失败: {e}") from e

    # 用量统计
    usage = getattr(provider, "last_usage", {}) or {}
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    cost = usage.get("cost_usd")
    if cost is None:
        cost = estimate_cost(cfg.model_name, pt, ct)
    call_stats.record(
        config=cfg, username=sess.username,
        prompt_tokens=pt, completion_tokens=ct,
        success=True, cost_usd=float(cost),
    )
    return {"text": text}


@app.post("/llm/chat")
def chat(req: ChatRequest, sess=Depends(get_current_session)):
    # 1) 找用户绑定的 LLM 配置
    provider, cfg = _resolve_user_provider(sess.username)

    # 2) 跑 dispatcher,出错也要记一条失败统计
    task_id = dispatcher.create(sess.username)
    dispatcher.mark_running(task_id)
    try:
        resp = provider.chat(system=req.system, user=req.user)
    except Exception as e:
        dispatcher.fail(task_id, str(e))
        call_stats.record(
            config=cfg, username=sess.username,
            prompt_tokens=0, completion_tokens=0,
            success=False, cost_usd=0.0,
        )
        raise HTTPException(500, f"LLM 调用失败: {e}") from e

    # 3) 记录用量
    usage = getattr(provider, "last_usage", {}) or {}
    pt = int(usage.get("prompt_tokens", 0) or 0)
    ct = int(usage.get("completion_tokens", 0) or 0)
    # OpenRouter 自带 cost;其他用表估
    cost = usage.get("cost_usd")
    if cost is None:
        cost = estimate_cost(cfg.model_name, pt, ct)
    call_stats.record(
        config=cfg, username=sess.username,
        prompt_tokens=pt, completion_tokens=ct,
        success=True, cost_usd=float(cost),
    )

    dispatcher.complete(task_id, resp.model_dump())
    return resp.model_dump()
