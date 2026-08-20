"""会话列表、基础 CRUD 和消息读取路由。"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request


def build_sessions_router(
    *,
    sessions: Any,
    task_store: Any,
    missed_store: Any,
    session_for_user: Callable[[str], Any],
    current_username: Callable[[], str],
    is_logged_in: Callable[[], bool],
    need_login: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/sessions", tags=["sessions"])

    @router.get("")
    def list_sessions():
        if not is_logged_in():
            return need_login()
        username = current_username()
        pending_misses = missed_store.list_pending(username)
        output = []
        for session in sessions.list_for(username):
            if getattr(session, "hidden", False):
                continue
            task_id = getattr(session, "task_id", "")
            task_needs_data = False
            missed_count = 0
            if task_id:
                task = task_store.get(task_id)
                if task:
                    task_needs_data = bool(task.needs_approval)
                missed_count = sum(1 for item in pending_misses if item.task_id == task_id)
            running = any(
                message.role == "assistant"
                and message.status in ("pending", "running", "awaiting_decrypt")
                for message in session.messages
            )
            output.append({
                "id": session.id,
                "title": session.title,
                "kind": getattr(session, "kind", "normal"),
                "task_id": task_id,
                "task_needs_data": task_needs_data,
                "missed_count": missed_count,
                "running": running,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "message_count": len(session.messages),
            })
        return output

    @router.post("")
    def create_session():
        if not is_logged_in():
            return need_login()
        session = sessions.create(username=current_username())
        return {"id": session.id, "title": session.title}

    @router.delete("/{sid}")
    def delete_session(sid: str):
        if not is_logged_in():
            return need_login()
        session = session_for_user(sid)
        if getattr(session, "kind", "normal") == "scheduled" and getattr(
            session, "task_id", ""
        ):
            sessions.set_hidden(sid, True)
            return {"ok": True, "hidden": True}
        sessions.delete(sid)
        return {"ok": True, "hidden": False}

    @router.post("/{sid}/title")
    async def rename_session(sid: str, request: Request):
        if not is_logged_in():
            return need_login()
        session_for_user(sid)
        data = await request.json()
        sessions.rename(sid, data.get("title", ""))
        return {"ok": True}

    @router.get("/{sid}/messages")
    def list_messages(sid: str):
        if not is_logged_in():
            return need_login()
        session = session_for_user(sid)
        return {
            "session": {
                "id": session.id,
                "title": session.title,
                "kind": getattr(session, "kind", "normal"),
                "updated_at": session.updated_at,
            },
            "messages": [message.to_dict() for message in session.messages],
        }

    @router.get("/{sid}/messages/{mid}")
    def get_message(sid: str, mid: str):
        if not is_logged_in():
            return need_login()
        session = session_for_user(sid)
        for message in session.messages:
            if message.id == mid:
                return message.to_dict()
        raise HTTPException(404, "消息不存在")

    return router
