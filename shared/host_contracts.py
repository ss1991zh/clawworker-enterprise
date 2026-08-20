"""管理端与用户端之间的稳定数据契约。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _HostModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class NaturalQueryPlanResponse(_HostModel):
    sql: str = Field(min_length=1)
    explanation: str = ""


class QueryTaskResponse(_HostModel):
    task_id: str = Field(min_length=1)
    status: str = "queued"
    error: str = ""


class QueryResultResponse(_HostModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    request_id: str = ""
    duration_ms: int = 0
    max_result_bytes: int = 0


class EncryptedDatabaseMetadata(_HostModel):
    path: str = Field(min_length=1)
    name: str = ""
    row_count: int = 0
    encrypted_columns: list[str] = Field(default_factory=list)
    plaintext_columns: list[str] = Field(default_factory=list)
