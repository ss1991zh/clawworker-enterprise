"""用户端密钥体检和合规审计接口。"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from client.host_client import HostConnectionError, HostResponseError


def build_settings_router(
    *, is_logged_in, need_login, keystore, session_state, host_client,
    bind_runtime_vault, clear_local_session,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/keys")
    def api_keys_get():
        if not is_logged_in():
            return need_login()
        vault = keystore.vault_path(session_state["username"])

        def info(name: str):
            path = vault / name
            return path.exists(), (str(path) if path.exists() else "")

        sk_present, sk_path = info("sk.bin")
        evk_present, evk_path = info("evk.bin")
        auth_present, auth_path = info("user_authorization")
        dict_present, dict_path = info("dictf")
        return {
            "sk_present": sk_present, "sk_path": sk_path,
            "evk_present": evk_present, "evk_path": evk_path,
            "user_auth_present": auth_present, "user_auth_path": auth_path,
            "dict_present": dict_present, "dict_path": dict_path,
        }

    async def import_upload(file: UploadFile, importer):
        if not is_logged_in():
            return need_login()
        data = await file.read()
        if not data:
            raise HTTPException(400, "文件为空")
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            temp.write(data)
            temp_path = Path(temp.name)
        try:
            dst = importer(username=session_state["username"], source=temp_path)
            bind_runtime_vault()
            return {"ok": True, "path": str(dst), "size_bytes": dst.stat().st_size}
        finally:
            temp_path.unlink(missing_ok=True)

    @router.post("/api/keys/sk")
    async def api_keys_upload_sk(file: UploadFile = File(...)):
        return await import_upload(file, keystore.import_sk)

    @router.post("/api/keys/evk")
    async def api_keys_upload_evk(file: UploadFile = File(...)):
        return await import_upload(file, keystore.import_evk)

    @router.post("/api/keys/dict")
    async def api_keys_upload_dict(file: UploadFile = File(...)):
        return await import_upload(file, keystore.import_dict)

    @router.post("/api/keys/fetch_auth")
    def api_keys_fetch_auth():
        if not is_logged_in():
            return need_login()
        try:
            authorization = host_client.request_bytes("GET", "/auth/user_authorization", timeout=30)
        except HostResponseError as exc:
            if exc.status_code == 401:
                clear_local_session()
                raise HTTPException(502, "主机拒绝(session 已过期)· 请退出后重新登录") from exc
            raise HTTPException(502, f"主机拒绝:{exc.detail}") from exc
        except HostConnectionError as exc:
            raise HTTPException(502, str(exc)) from exc
        with tempfile.NamedTemporaryFile(delete=False) as temp:
            temp.write(authorization)
            temp_path = Path(temp.name)
        try:
            dst = keystore.import_user_authorization(
                username=session_state["username"], source=temp_path,
            )
            bind_runtime_vault()
            return {"ok": True, "path": str(dst), "size_bytes": dst.stat().st_size}
        finally:
            temp_path.unlink(missing_ok=True)

    @router.get("/api/keycheck")
    def api_keycheck(quick: bool = True):
        if not is_logged_in():
            return need_login()
        keys = keystore.get_paths(session_state["username"])
        if not (keys and keys.sk_path.exists()):
            raise HTTPException(400, "尚未导入密钥(SK)。请先导入密钥与字典,再做体检。")
        try:
            from client.tools.runtime import Runtime
            Runtime.get().ensure_all_initialized()
        except Exception as exc:
            raise HTTPException(400, f"密钥/字典初始化失败(可能不配套或损坏):{type(exc).__name__}: {exc}") from exc
        try:
            from client.he_ops.selfcheck import health_report
            return health_report(quick=quick)
        except Exception as exc:
            raise HTTPException(500, f"体检执行失败:{type(exc).__name__}: {exc}") from exc

    @router.get("/api/audit")
    def api_audit(limit: int = 200):
        if not is_logged_in():
            return need_login()
        from client.he_ops import audit
        user = session_state["username"]
        return {"summary": audit.summary(user), "events": audit.read_events(user, limit=limit)}

    @router.get("/api/audit/export")
    def api_audit_export():
        if not is_logged_in():
            return need_login()
        from client.he_ops import audit_report
        user = session_state["username"]
        try:
            data = audit_report.build_docx(user)
        except Exception as exc:
            raise HTTPException(500, f"生成报告失败:{type(exc).__name__}: {exc}") from exc
        filename = f"数据隐私合规报告_{datetime.now().strftime('%Y%m%d')}.docx"
        return Response(
            content=data,
            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
        )

    return router
