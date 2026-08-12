"""数据库访问策略与查询审计存储。第一版采用用户直授权，默认拒绝。"""
from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from host import secret_store


OPERATIONS = ("browse", "query", "analyze", "export")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class AccessPolicy:
    id: str
    username: str
    data_source_id: str
    schema_name: str
    table_name: str
    allowed_columns: tuple[str, ...]
    operations: tuple[str, ...]
    max_rows: int
    enabled: bool
    created_at: str
    updated_at: str

    def permits(self, operation: str) -> bool:
        return self.enabled and operation in self.operations

    def permits_column(self, column: str) -> bool:
        return "*" in self.allowed_columns or column.lower() in {
            item.lower() for item in self.allowed_columns
        }


class DataAccessStore:
    def __init__(self, db_path: Optional[Path] = None, *, harden=secret_store.harden_file) -> None:
        self.db_path = Path(db_path or os.environ.get("CLAWWORKER_CONTROL_DB", "")
                            or Path.home() / ".agent-system" / "host-data" / "control.db")
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._harden = harden
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self):
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS access_policies (
                    id TEXT PRIMARY KEY,
                    username TEXT NOT NULL,
                    data_source_id TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    allowed_columns_json TEXT NOT NULL,
                    operations_json TEXT NOT NULL,
                    max_rows INTEGER NOT NULL DEFAULT 10000,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(username,data_source_id,schema_name,table_name),
                    FOREIGN KEY(data_source_id) REFERENCES data_sources(id) ON DELETE CASCADE,
                    CHECK(max_rows BETWEEN 1 AND 1000000)
                );
                CREATE INDEX IF NOT EXISTS ix_policy_user_source
                    ON access_policies(username,data_source_id,enabled);

                CREATE TABLE IF NOT EXISTS query_audits (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    data_source_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    sql_text TEXT NOT NULL,
                    tables_json TEXT NOT NULL,
                    columns_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    row_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_audit_user_time
                    ON query_audits(username,created_at DESC);
                """
            )
            self._harden(self.db_path)

    @staticmethod
    def _row(row) -> AccessPolicy:
        return AccessPolicy(
            id=row["id"], username=row["username"], data_source_id=row["data_source_id"],
            schema_name=row["schema_name"], table_name=row["table_name"],
            allowed_columns=tuple(json.loads(row["allowed_columns_json"])),
            operations=tuple(json.loads(row["operations_json"])), max_rows=int(row["max_rows"]),
            enabled=bool(row["enabled"]), created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def grant(self, *, username: str, data_source_id: str, schema_name: str,
              table_name: str, allowed_columns: list[str], operations: list[str],
              max_rows: int = 10000) -> AccessPolicy:
        username, schema_name, table_name = username.strip(), schema_name.strip(), table_name.strip()
        columns = sorted({c.strip() for c in allowed_columns if c.strip()}, key=str.lower)
        ops = sorted({o.strip() for o in operations if o.strip()})
        if not username or not schema_name or not table_name:
            raise ValueError("用户、Schema 和表名不能为空")
        if not columns:
            raise ValueError("至少授权一个字段；使用 * 表示整表字段")
        if not ops or any(o not in OPERATIONS for o in ops):
            raise ValueError("操作权限无效")
        if not 1 <= int(max_rows) <= 1_000_000:
            raise ValueError("权限最大行数必须在 1～1,000,000 之间")
        policy_id, now = secrets.token_hex(8), _now()
        with self._lock, self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM data_sources WHERE id=?", (data_source_id,)).fetchone()
            if not exists:
                raise ValueError("数据源不存在")
            conn.execute(
                """INSERT INTO access_policies
                   (id,username,data_source_id,schema_name,table_name,allowed_columns_json,
                    operations_json,max_rows,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,1,?,?)
                   ON CONFLICT(username,data_source_id,schema_name,table_name) DO UPDATE SET
                     allowed_columns_json=excluded.allowed_columns_json,
                     operations_json=excluded.operations_json,max_rows=excluded.max_rows,
                     enabled=1,updated_at=excluded.updated_at""",
                (policy_id, username, data_source_id, schema_name, table_name,
                 json.dumps(columns, ensure_ascii=False), json.dumps(ops), int(max_rows), now, now),
            )
            row = conn.execute(
                """SELECT * FROM access_policies WHERE username=? AND data_source_id=?
                   AND schema_name=? AND table_name=?""",
                (username, data_source_id, schema_name, table_name),
            ).fetchone()
        return self._row(row)

    def revoke(self, policy_id: str) -> None:
        with self._lock, self._connect() as conn:
            if conn.execute("DELETE FROM access_policies WHERE id=?", (policy_id,)).rowcount != 1:
                raise ValueError("权限不存在")

    def list_for_user(self, username: str, data_source_id: str = "") -> list[AccessPolicy]:
        sql = "SELECT * FROM access_policies WHERE username=? AND enabled=1"
        params: list[str] = [username]
        if data_source_id:
            sql += " AND data_source_id=?"
            params.append(data_source_id)
        sql += " ORDER BY data_source_id,schema_name,table_name"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row(row) for row in rows]

    def list_all(self) -> list[AccessPolicy]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM access_policies ORDER BY username,data_source_id,schema_name,table_name"
            ).fetchall()
        return [self._row(row) for row in rows]

    def record_audit(self, *, request_id: str, username: str, data_source_id: str,
                     operation: str, sql_text: str, tables: list[str], columns: list[str],
                     status: str, row_count: int = 0, duration_ms: int = 0,
                     error_code: str = "") -> str:
        audit_id = secrets.token_hex(10)
        with self._lock, self._connect() as conn:
            conn.execute(
                """INSERT INTO query_audits
                   (id,request_id,username,data_source_id,operation,sql_text,tables_json,
                    columns_json,status,row_count,duration_ms,error_code,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (audit_id, request_id, username, data_source_id, operation, sql_text[:20000],
                 json.dumps(tables, ensure_ascii=False), json.dumps(columns, ensure_ascii=False),
                 status, int(row_count), int(duration_ms), error_code[:100], _now()),
            )
        return audit_id

    def list_audits(self, limit: int = 200) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM query_audits ORDER BY created_at DESC LIMIT ?", (min(max(limit, 1), 1000),)
            ).fetchall()
        return [dict(row) for row in rows]
