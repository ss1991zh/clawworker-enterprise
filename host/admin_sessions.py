"""管理端在线会话和用户记录路由。"""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse


def _mask_token(token: str, keep: int = 6) -> str:
    if not token:
        return "—"
    if len(token) <= keep * 2:
        return "*" * len(token)
    return f"{token[:keep]}...{token[-keep:]}"


def build_sessions_router(*, templates, user_manager, flash_redirect, pop_messages) -> APIRouter:
    router = APIRouter()

    def safe_records(fn, default):
        try:
            return fn()
        except Exception:
            return default

    def safe_back(url: str) -> str:
        return url if (url or "").startswith("/admin/") else "/admin/sessions"

    @router.get("/sessions", response_class=HTMLResponse)
    def session_list(request: Request):
        from host import client_records as cr
        users = safe_records(cr.list_users, [])
        sessions = [{
            "username": session.username,
            "token": session.token,
            "token_masked": _mask_token(session.token),
            "expires_at": session.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
        } for session in user_manager._sessions.values()]
        return templates.TemplateResponse(
            request, "sessions.html",
            {"active": "sessions", "users": users, "sessions": sessions,
             "messages": pop_messages(request)},
        )

    @router.get("/sessions/user/{username}", response_class=HTMLResponse)
    def user_records(request: Request, username: str):
        from host import client_records as cr
        show_hidden = request.query_params.get("show_hidden") == "1"
        chats = [s for s in safe_records(cr.list_sessions, []) if s["username"] == username]
        tasks = [t for t in safe_records(cr.list_tasks, []) if t["username"] == username]
        hidden_n = sum(1 for row in chats if row["hidden"]) + sum(1 for row in tasks if row["hidden"])
        if not show_hidden:
            chats = [row for row in chats if not row["hidden"]]
            tasks = [row for row in tasks if not row["hidden"]]
        tab = "scheduled" if request.query_params.get("tab") == "scheduled" else "normal"
        normal_chats = [chat for chat in chats if chat["kind"] != "scheduled"]
        sess_by_task = {session["task_id"]: session["id"]
                        for session in safe_records(cr.list_sessions, []) if session.get("task_id")}
        selected = None
        selected_task = None
        if tab == "scheduled":
            task_id = request.query_params.get("t", "")
            if task_id and any(task["id"] == task_id for task in tasks):
                selected_task = next(task for task in tasks if task["id"] == task_id)
            elif tasks:
                selected_task = tasks[0]
            if selected_task:
                session_id = sess_by_task.get(selected_task["id"])
                selected = cr.get_session(session_id) if session_id else None
        else:
            session_id = request.query_params.get("s", "")
            if session_id and any(chat["id"] == session_id for chat in normal_chats):
                selected = cr.get_session(session_id)
            elif normal_chats:
                selected = cr.get_session(normal_chats[0]["id"])
        return templates.TemplateResponse(
            request, "user_records.html",
            {"active": "sessions", "username": username, "tab": tab,
             "normal_chats": normal_chats, "tasks": tasks, "sel": selected,
             "sel_task": selected_task, "normal_n": len(normal_chats),
             "show_hidden": show_hidden, "hidden_n": hidden_n,
             "messages": pop_messages(request)},
        )

    @router.post("/sessions/{token}/revoke")
    def session_revoke(request: Request, token: str):
        user_manager.logout(token)
        return flash_redirect("/admin/sessions", ("success", "登录会话已注销"))

    @router.post("/records/{kind}/{rid}/hide")
    def record_hide(request: Request, kind: str, rid: str, back: str = Form("")):
        from host import client_records as cr
        if kind not in ("session", "task"):
            return flash_redirect("/admin/sessions", ("error", "未知记录类型"))
        cr.hide(f"{kind}:{rid}")
        return flash_redirect(safe_back(back), ("success", "已从列表隐藏(仅本页显示,用户侧不受影响)"))

    @router.post("/records/{kind}/{rid}/unhide")
    def record_unhide(request: Request, kind: str, rid: str, back: str = Form("")):
        from host import client_records as cr
        if kind not in ("session", "task"):
            return flash_redirect("/admin/sessions", ("error", "未知记录类型"))
        cr.unhide(f"{kind}:{rid}")
        return flash_redirect(safe_back(back), ("success", "已恢复显示"))

    return router
