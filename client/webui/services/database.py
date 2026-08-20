"""企业数据库结果的本机加密与查询任务编排。"""
from __future__ import annotations

import re
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from pydantic import ValidationError

from shared.host_contracts import NaturalQueryPlanResponse, QueryTaskResponse


class DatabaseResultEncryptionError(ValueError):
    """数据库查询成功，但结果在用户电脑本地加密失败。"""


HostApi = Callable[..., Any]
IngestPlaintext = Callable[..., dict]


class DatabaseAnalysisService:
    """保证数据库明文只在用户电脑的受控内存和临时文件中短暂停留。"""

    TERMINAL_STATUSES = frozenset({"success", "failed", "cancelled", "timeout", "denied"})

    def __init__(
        self,
        host_api: HostApi,
        ingest_plaintext: IngestPlaintext,
        *,
        named_temporary_file: Callable[..., Any] = tempfile.NamedTemporaryFile,
        result_encryptor: Callable[[dict, str], dict] | None = None,
    ) -> None:
        self._host_api = host_api
        self._ingest_plaintext = ingest_plaintext
        self._named_temporary_file = named_temporary_file
        self._result_encryptor = result_encryptor

    def encrypt_result(self, result: dict, *, data_source_id: str) -> dict:
        """立即把管理端返回的数据行摄取为本地密文，只返回密文元数据。"""
        import pandas as pd

        columns = result.get("columns") or []
        rows = result.pop("rows", []) or []
        frame = pd.DataFrame(rows, columns=columns)
        source_id = re.sub(r"[^A-Za-z0-9_-]+", "_", str(data_source_id or "db"))[:24]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        with self._named_temporary_file(delete=False, suffix=".xlsx") as tmp:
            tmp_path = Path(tmp.name)
        try:
            frame.to_excel(tmp_path, index=False)
            max_result_bytes = int(result.get("max_result_bytes", 0) or 0)
            if max_result_bytes and tmp_path.stat().st_size > max_result_bytes:
                raise ValueError(
                    f"本地 Excel 文件超过管理员设置的 {max_result_bytes // 1048576} MB 限制"
                )
            encrypted = self._ingest_plaintext(
                tmp_path,
                f"数据库提取_{source_id}_{stamp}.xlsx",
                dst_stem=f"db_{source_id}_{stamp}",
            )
        except ValueError as exc:
            raise DatabaseResultEncryptionError(str(exc)) from exc
        finally:
            tmp_path.unlink(missing_ok=True)
            frame = None
            rows = None

        response = {
            "request_id": result.get("request_id", ""),
            "row_count": result.get("row_count", encrypted.get("row_count", 0)),
            "duration_ms": result.get("duration_ms", 0),
            "columns": columns,
            "encrypted": encrypted,
        }
        assert "rows" not in response
        return response

    def prepare_cipher(
        self,
        *,
        data_source_id: str,
        intent: str,
        on_step: Callable[[str, str], None],
        should_cancel: Callable[[], bool],
    ) -> dict:
        """规划、执行、取回查询，并在进入分析 Pipeline 前立即转成密文。"""
        on_step("database", "正在根据授权表结构理解查询需求")
        plan = self._host_api(
            "POST",
            "/data/query/plan",
            json_body={
                "data_source_id": data_source_id,
                "intent": intent,
                "operation": "query",
            },
            timeout=120.0,
        )
        try:
            plan_contract = NaturalQueryPlanResponse.model_validate(plan)
        except ValidationError as exc:
            raise RuntimeError("管理端返回的数据库查询方案缺少有效 SQL") from exc
        if should_cancel():
            return {"cancelled": True}

        on_step("database", "正在通过管理端安全网关查询数据")
        task = self._host_api(
            "POST",
            "/data/query/tasks",
            json_body={
                "data_source_id": data_source_id,
                "sql": plan_contract.sql,
                "operation": "query",
            },
        )
        try:
            task_contract = QueryTaskResponse.model_validate(task)
        except ValidationError as exc:
            raise RuntimeError("管理端未返回有效的数据库查询任务编号") from exc
        task_id = task_contract.task_id

        status = task
        while status.get("status") not in self.TERMINAL_STATUSES:
            if should_cancel():
                try:
                    self._host_api("DELETE", f"/data/query/tasks/{quote(task_id, safe='')}")
                finally:
                    return {"cancelled": True}
            time.sleep(0.35)
            status = self._host_api("GET", f"/data/query/tasks/{quote(task_id, safe='')}")

        if status.get("status") != "success":
            labels = {
                "cancelled": "数据库查询已取消",
                "timeout": "数据库查询已超时",
                "denied": "数据库查询被安全策略拒绝",
                "failed": "数据库查询失败",
            }
            detail = str(status.get("error") or "")
            raise RuntimeError(
                f"{labels.get(status.get('status'), '数据库查询未完成')}"
                f"{('：' + detail) if detail else ''}"
            )

        on_step("database", "查询结果已返回本机，正在立即加密")
        plaintext_result = self._host_api(
            "POST",
            f"/data/query/tasks/{quote(task_id, safe='')}/result",
            timeout=300.0,
        )
        if not isinstance(plaintext_result, dict):
            raise RuntimeError("管理端返回的数据库结果格式无效")
        encrypted_response = (
            self._result_encryptor(plaintext_result, data_source_id)
            if self._result_encryptor is not None
            else self.encrypt_result(plaintext_result, data_source_id=data_source_id)
        )
        encrypted = dict(encrypted_response.get("encrypted") or {})
        cipher_path = str(encrypted.get("path") or "")
        if not cipher_path or not Path(cipher_path).exists():
            raise RuntimeError("数据库结果已查询，但本地密文文件未生成")

        safe = {
            "path": cipher_path,
            "name": str(encrypted.get("name") or Path(cipher_path).name),
            "row_count": int(encrypted_response.get("row_count", 0) or 0),
            "encrypted_columns": list(encrypted.get("encrypted_columns") or []),
            "plaintext_columns": list(encrypted.get("plaintext_columns") or []),
        }
        assert "rows" not in safe and "sql" not in safe
        return safe
