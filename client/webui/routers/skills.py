"""内置、教学和用户自定义技能管理路由。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from client import skills_loader
from client.webui.skills_store import CustomSkillStore, builtin_skills


def build_skills_router(
    *,
    custom_skills: CustomSkillStore,
    is_logged_in: Callable[[], bool],
    need_login: Callable[[], Any],
) -> APIRouter:
    router = APIRouter(prefix="/api/skills", tags=["skills"])

    @router.get("")
    def list_skills():
        if not is_logged_in():
            return need_login()
        return {
            "skill_md": skills_loader.list_meta(),
            "builtin": builtin_skills(),
            "custom": custom_skills.list_all(),
        }

    @router.get("/md/{slug}")
    def skill_body(slug: str):
        if not is_logged_in():
            return need_login()
        body = skills_loader.get_body(slug)
        if body is None:
            raise HTTPException(404, "skill 不存在")
        return {"slug": slug, "body": body}

    @router.post("/upload")
    async def upload_skill(
        files: list[UploadFile] = File(...),
        paths: list[str] = Form(default=[]),
    ):
        if not is_logged_in():
            return need_login()
        if not files:
            raise HTTPException(400, "没有文件")
        try:
            if len(files) == 1 and (files[0].filename or "").lower().endswith(".zip"):
                doc = skills_loader.add_user_skill_zip(await files[0].read())
            elif (
                len(files) == 1
                and (files[0].filename or "").lower().endswith(".md")
                and not paths
            ):
                data = await files[0].read()
                doc = skills_loader.add_user_skill_md(
                    data.decode("utf-8", errors="replace"),
                    fallback_name=Path(files[0].filename or "skill").stem,
                )
            else:
                collected: list[tuple[str, bytes]] = []
                for index, upload in enumerate(files):
                    relative = (
                        paths[index]
                        if index < len(paths) and paths[index]
                        else (upload.filename or f"file_{index}")
                    )
                    collected.append((relative, await upload.read()))
                doc = skills_loader.add_user_skill_files(collected)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(400, f"技能包解析失败:{type(exc).__name__}: {exc}") from exc
        return doc.to_meta()

    @router.delete("/md/{slug}")
    def delete_skill(slug: str):
        if not is_logged_in():
            return need_login()
        if not skills_loader.delete_user_skill(slug):
            raise HTTPException(404, "用户技能不存在(内置技能不可删)")
        return {"ok": True}

    return router
