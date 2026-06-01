"""
主机 HTTP 服务(FastAPI)。

提供两类端点:
- /auth/login           账号密码登录,返回 session token
- /llm/chat             调用 LLM 代理(需 session token)

启动:
    uvicorn host.server:app --host 0.0.0.0 --port 8443
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from host.admin_ui import build_admin_router
from host.cert_manager import AuthorizationManager
from host.dispatcher import Dispatcher
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
llm_provider: Optional[LLMProvider] = None  # 在 startup 时初始化


def _init_llm_provider() -> Optional[LLMProvider]:
    """
    根据环境变量初始化 LLM provider。

    必填:MODEL_TYPE(stub / anthropic / openrouter / openai)
    可选:MODEL_NAME(默认按 provider 而异)
    密钥:ANTHROPIC_API_KEY / OPENROUTER_API_KEY / OPENAI_API_KEY(按 type 取)
    """
    model_type = os.environ.get("MODEL_TYPE", "")
    if not model_type:
        return None  # 未配置,/llm/chat 会返回 503

    if model_type == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            return None
        return make_provider(
            "anthropic",
            api_key=api_key,
            model=os.environ.get("MODEL_NAME", "claude-sonnet-4-5"),
        )

    if model_type == "openrouter":
        api_key = os.environ.get("OPENROUTER_API_KEY")
        if not api_key:
            return None
        return make_provider(
            "openrouter",
            api_key=api_key,
            model=os.environ.get("MODEL_NAME", "deepseek/deepseek-v4-pro"),
        )

    if model_type == "openai":
        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        return make_provider(
            "openai",
            api_key=api_key,
            model=os.environ.get("MODEL_NAME", "gpt-4o"),
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        )

    return None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="agent-system host", version="0.1.0")

# Admin UI 路由 + 静态文件
from pathlib import Path as _P

_HOST_DIR = _P(__file__).resolve().parent
app.mount("/admin/static", StaticFiles(directory=str(_HOST_DIR / "static")), name="admin_static")
app.include_router(
    build_admin_router(
        auth_manager=auth_manager,
        user_manager=user_manager,
        dispatcher=dispatcher,
        get_llm_provider=lambda: llm_provider,
    )
)


@app.on_event("startup")
def startup():
    global llm_provider
    llm_provider = _init_llm_provider()


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


@app.post("/admin/account/create")
def create_account(req: CreateAccountRequest):
    try:
        acct = user_manager.create_account(
            username=req.username, password=req.password, auth_id=req.auth_id
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"username": acct.username, "status": acct.status}


# ----- LLM 代理 -----


class ChatRequest(BaseModel):
    system: str
    user: str


@app.post("/llm/chat")
def chat(req: ChatRequest, sess=Depends(get_current_session)):
    if llm_provider is None:
        raise HTTPException(503, "LLM provider 未配置(请设置 MODEL_TYPE 与 API key)")
    task_id = dispatcher.create(sess.username)
    dispatcher.mark_running(task_id)
    try:
        resp = llm_provider.chat(system=req.system, user=req.user)
    except Exception as e:
        dispatcher.fail(task_id, str(e))
        raise HTTPException(500, f"LLM 调用失败: {e}") from e
    dispatcher.complete(task_id, resp.model_dump())
    return resp.model_dump()
