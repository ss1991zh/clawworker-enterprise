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

from fastapi import Depends, FastAPI, Header, HTTPException, Request
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

from host.admin_auth import AdminAuth, COOKIE as _ADMIN_COOKIE
from host.login_throttle import LoginThrottle
admin_auth = AdminAuth()
_login_throttle = LoginThrottle()   # 用户登录 + admin 登录共用


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
        admin_auth=admin_auth,
        login_throttle=_login_throttle,
    )
)


# 根路径 → 管理后台(裸访问 :8443 时不再 404;登录闸门交给下面中间件)
@app.get("/")
def _root():
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin", status_code=303)


# Admin 登录闸门:未登录访问 /admin/* 一律跳登录页(登录页 / 静态资源放行)
@app.middleware("http")
async def _admin_login_gate(request, call_next):
    p = request.url.path
    if (p == "/admin" or p.startswith("/admin/")) and not (
        p.startswith("/admin/static") or p == "/admin/login" or p == "/admin/logout"
    ):
        if not admin_auth.valid(request.cookies.get(_ADMIN_COOKIE)):
            from fastapi.responses import RedirectResponse
            return RedirectResponse("/admin/login", status_code=303)
    return await call_next(request)


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
def login(req: LoginRequest, request: Request):
    # 限速:按 用户名 + 来源IP 双 key,防在线爆破
    ip = request.client.host if request.client else "?"
    keys = [f"u:{req.username}", f"ip:{ip}"]
    wait = max(_login_throttle.check(k) for k in keys)
    if wait > 0:
        raise HTTPException(429, f"登录尝试过于频繁,请 {int(wait) + 1} 秒后再试")
    try:
        sess = user_manager.login(username=req.username, password=req.password)
    except PermissionError as e:
        for k in keys:
            _login_throttle.record_failure(k)
        raise HTTPException(401, str(e))
    for k in keys:
        _login_throttle.record_success(k)
    return {"token": sess.token, "expires_at": sess.expires_at.isoformat()}


@app.get("/tls/fingerprint")
def tls_fingerprint():
    """
    返回主机 TLS 证书指纹(公开信息,供客户端首次登记时人工核对,防中间人)。
    admin 也可在后台看到同一指纹,读给终端用户核对。
    """
    try:
        from host import tls_cert
        return {"fingerprint": tls_cert.current_fingerprint()}
    except Exception:  # noqa: BLE001
        return {"fingerprint": None}


@app.post("/client/report-init-failed")
def report_init_failed(sess=Depends(get_current_session)):
    """
    客户端 SDK initDict 失败(授权过期/被吊销/损坏)时上报。
    主机标记该用户授权失效 → 后续登录/取证书被拒,并在 admin 状态可见。
    闭合"过期 → 客户端 init 失败 → 上报 → 自动 disable"链路。
    """
    auth_manager.report_init_failed(sess.username)
    return {"ok": True, "username": sess.username}


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


class ChatTurn(BaseModel):
    role: str  # "user" / "assistant"
    content: str


class ChatRequest(BaseModel):
    system: str
    user: str
    # 可选:同会话的近端历史(由客户端裁剪);后端会折叠到 user prompt 顶部
    history: list[ChatTurn] = []
    # 可选:启用联网搜索(若该用户绑定的模型/服务支持;不支持自动降级为普通调用)
    web_search: bool = False


def _compose_user_with_history(history: list[ChatTurn], new_user: str) -> str:
    """把 history 折叠到一段 user prompt 里 —— provider 接口不变。"""
    if not history:
        return new_user
    parts = ["[最近对话历史 · 仅供上下文参考,不一定与本次问题相关]"]
    for t in history:
        role = "用户" if t.role == "user" else "助手"
        content = (t.content or "").strip()
        if not content:
            continue
        # 截断每条 600 字,避免历史过长把 prompt 撑爆
        parts.append(f"{role}: {content[:600]}")
    parts.append("")
    parts.append("[本次新消息]")
    parts.append(new_user)
    return "\n".join(parts)


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
    user_msg = _compose_user_with_history(req.history, req.user)
    try:
        text = provider.raw_chat(system=req.system, user=user_msg, web_search=req.web_search)
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
    return {"text": text,
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}


@app.post("/llm/chat")
def chat(req: ChatRequest, sess=Depends(get_current_session)):
    # 1) 找用户绑定的 LLM 配置
    provider, cfg = _resolve_user_provider(sess.username)

    # 2) 拆成两步:先 raw_chat 拿文本,再 parse_llm_text 抽计划。
    #    这样 LLM 把 plan 给糊了也能把 raw 文本打日志、给前端友好提示
    from host.llm_proxy import parse_llm_text

    task_id = dispatcher.create(sess.username)
    dispatcher.mark_running(task_id)

    # 2a) 先调用 LLM(把会话历史折叠到 user prompt 顶部)
    user_msg = _compose_user_with_history(req.history, req.user)
    try:
        raw_text = provider.raw_chat(system=req.system, user=user_msg)
    except Exception as e:
        dispatcher.fail(task_id, str(e))
        call_stats.record(
            config=cfg, username=sess.username,
            prompt_tokens=0, completion_tokens=0,
            success=False, cost_usd=0.0,
        )
        raise HTTPException(500, f"LLM 调用失败: {e}") from e

    # 2b) 再 parse — 失败时把原文留日志,前端给简洁提示
    # 空字符串特判:推理模型可能 reasoning_tokens 把 max_tokens 吃光,visible content 是空
    if not raw_text or not raw_text.strip():
        usage = getattr(provider, "last_usage", {}) or {}
        print(
            f"[/llm/chat] EMPTY response · user={sess.username} · cfg={cfg.name}\n"
            f"  usage: {usage}\n"
            f"  提示:推理型模型(deepseek-v4-pro / o1)的 reasoning_tokens 可能用光 max_tokens"
        )
        call_stats.record(
            config=cfg, username=sess.username,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            success=False,
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
        )
        raise HTTPException(
            500,
            "LLM 没返回任何文本(推理型模型可能 reasoning_tokens 用光 max_tokens 配额)· "
            "解决方案:① 在 admin LLM 配置里把 max_tokens 调高(已默认 16000);"
            "② 换非推理模型(deepseek-chat、gpt-4o-mini);③ 把 system prompt 写短一点",
        )
    try:
        resp = parse_llm_text(raw_text)
    except ValueError as e:
        print(
            f"[/llm/chat] parse fail · user={sess.username} · cfg={cfg.name}\n"
            f"  err: {e}\n"
            f"  raw (前 600 字): {raw_text[:600]!r}\n"
            f"  raw (末 200 字): {raw_text[-200:]!r}"
        )
        dispatcher.fail(task_id, str(e))
        # 用 raw_chat 时 last_usage 已填,这里也记一条失败统计但保留 token 用量
        usage = getattr(provider, "last_usage", {}) or {}
        call_stats.record(
            config=cfg, username=sess.username,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            success=False,
            cost_usd=float(usage.get("cost_usd", 0.0) or 0.0),
        )
        raise HTTPException(
            500,
            f"LLM 输出不符合规范(未找到 computation_plan)· "
            f"模型可能没按 system prompt 输出 JSON · "
            f"请稍后再试 / 换个模型 / 重写问题",
        ) from e

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
    return {**resp.model_dump(),
            "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct}}
