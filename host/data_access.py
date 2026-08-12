"""数据库访问策略、用户组、行级范围、字段脱敏与查询审计存储。"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from host import secret_store


OPERATIONS = ("browse", "query", "analyze", "export")


def redact_audit_sql(sql_text: str) -> str:
    """审计保留 SQL 结构，但不长期保存条件中的业务字面值。"""
    text = (sql_text or "")[:20000]
    # SQL 字符串常量（支持单引号用两个单引号转义）。双引号可能是标识符，不处理。
    text = re.sub(r"'(?:''|[^'])*'", "'***'", text)
    # 数值常量脱敏；保留 LIMIT/TOP 的行数值便于管理员判断提取规模。
    text = re.sub(
        r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])",
        lambda match: match.group(1)
        if re.search(r"(?:LIMIT|TOP)\s*$", text[max(0, match.start()-16):match.start()], re.I)
        else "?",
        text,
    )
    return text


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
    row_filter_sql: str
    masked_columns: dict[str, str]
    enabled: bool
    created_at: str
    updated_at: str

    def permits(self, operation: str) -> bool:
        return self.enabled and operation in self.operations

    def permits_column(self, column: str) -> bool:
        return "*" in self.allowed_columns or column.lower() in {
            item.lower() for item in self.allowed_columns
        }

    def mask_for(self, column: str) -> str:
        return self.masked_columns.get(column.lower(), "")


@dataclass(frozen=True)
class AccessGroup:
    id: str
    name: str
    description: str
    members: tuple[str, ...]
    created_at: str
    updated_at: str


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
                    row_filter_sql TEXT NOT NULL DEFAULT '',
                    masked_columns_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(username,data_source_id,schema_name,table_name),
                    FOREIGN KEY(data_source_id) REFERENCES data_sources(id) ON DELETE CASCADE,
                    CHECK(max_rows BETWEEN 1 AND 1000000)
                );
                CREATE INDEX IF NOT EXISTS ix_policy_user_source
                    ON access_policies(username,data_source_id,enabled);

                CREATE TABLE IF NOT EXISTS access_groups (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    description TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS access_group_members (
                    group_id TEXT NOT NULL,
                    username TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(group_id,username),
                    FOREIGN KEY(group_id) REFERENCES access_groups(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_group_member_user
                    ON access_group_members(username,group_id);
                CREATE TABLE IF NOT EXISTS group_access_policies (
                    id TEXT PRIMARY KEY,
                    group_id TEXT NOT NULL,
                    data_source_id TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    allowed_columns_json TEXT NOT NULL,
                    operations_json TEXT NOT NULL,
                    max_rows INTEGER NOT NULL DEFAULT 10000,
                    row_filter_sql TEXT NOT NULL DEFAULT '',
                    masked_columns_json TEXT NOT NULL DEFAULT '{}',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(group_id,data_source_id,schema_name,table_name),
                    FOREIGN KEY(group_id) REFERENCES access_groups(id) ON DELETE CASCADE,
                    FOREIGN KEY(data_source_id) REFERENCES data_sources(id) ON DELETE CASCADE,
                    CHECK(max_rows BETWEEN 1 AND 1000000)
                );
                CREATE INDEX IF NOT EXISTS ix_group_policy_source
                    ON group_access_policies(group_id,data_source_id,enabled);

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
            # 兼容 1.5.0 之前已经创建的控制库。
            existing = {row[1] for row in conn.execute("PRAGMA table_info(access_policies)")}
            if "row_filter_sql" not in existing:
                conn.execute("ALTER TABLE access_policies ADD COLUMN row_filter_sql TEXT NOT NULL DEFAULT ''")
            if "masked_columns_json" not in existing:
                conn.execute("ALTER TABLE access_policies ADD COLUMN masked_columns_json TEXT NOT NULL DEFAULT '{}'")
            self._harden(self.db_path)

    @staticmethod
    def _row(row) -> AccessPolicy:
        return AccessPolicy(
            id=row["id"], username=row["username"], data_source_id=row["data_source_id"],
            schema_name=row["schema_name"], table_name=row["table_name"],
            allowed_columns=tuple(json.loads(row["allowed_columns_json"])),
            operations=tuple(json.loads(row["operations_json"])), max_rows=int(row["max_rows"]),
            row_filter_sql=row["row_filter_sql"] or "",
            masked_columns={str(k).lower(): str(v) for k, v in
                            json.loads(row["masked_columns_json"] or "{}").items()},
            enabled=bool(row["enabled"]), created_at=row["created_at"], updated_at=row["updated_at"],
        )

    def grant(self, *, username: str, data_source_id: str, schema_name: str,
              table_name: str, allowed_columns: list[str], operations: list[str],
              max_rows: int = 10000, row_filter_sql: str = "",
              masked_columns: Optional[dict[str, str]] = None) -> AccessPolicy:
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
        row_filter_sql = row_filter_sql.strip()
        masks = self._normalize_masks(masked_columns or {}, columns)
        policy_id, now = secrets.token_hex(8), _now()
        with self._lock, self._connect() as conn:
            exists = conn.execute("SELECT 1 FROM data_sources WHERE id=?", (data_source_id,)).fetchone()
            if not exists:
                raise ValueError("数据源不存在")
            conn.execute(
                """INSERT INTO access_policies
                   (id,username,data_source_id,schema_name,table_name,allowed_columns_json,
                    operations_json,max_rows,row_filter_sql,masked_columns_json,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)
                   ON CONFLICT(username,data_source_id,schema_name,table_name) DO UPDATE SET
                     allowed_columns_json=excluded.allowed_columns_json,
                     operations_json=excluded.operations_json,max_rows=excluded.max_rows,
                     row_filter_sql=excluded.row_filter_sql,
                     masked_columns_json=excluded.masked_columns_json,
                     enabled=1,updated_at=excluded.updated_at""",
                (policy_id, username, data_source_id, schema_name, table_name,
                 json.dumps(columns, ensure_ascii=False), json.dumps(ops), int(max_rows),
                 row_filter_sql, json.dumps(masks, ensure_ascii=False), now, now),
            )
            row = conn.execute(
                """SELECT * FROM access_policies WHERE username=? AND data_source_id=?
                   AND schema_name=? AND table_name=?""",
                (username, data_source_id, schema_name, table_name),
            ).fetchone()
        return self._row(row)

    @staticmethod
    def _normalize_masks(masked_columns: dict[str, str], allowed_columns: list[str]) -> dict[str, str]:
        valid = {"partial", "hash", "null"}
        allowed = {item.lower() for item in allowed_columns}
        result = {}
        for column, strategy in masked_columns.items():
            column, strategy = column.strip().lower(), strategy.strip().lower()
            if not column or strategy not in valid:
                raise ValueError("脱敏策略仅支持 partial、hash、null")
            if "*" not in allowed and column not in allowed:
                raise ValueError(f"脱敏字段 {column} 不在授权字段中")
            result[column] = strategy
        return result

    def create_group(self, name: str, description: str = "") -> AccessGroup:
        name, description = name.strip(), description.strip()
        if not name:
            raise ValueError("用户组名称不能为空")
        group_id, now = secrets.token_hex(8), _now()
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "INSERT INTO access_groups(id,name,description,created_at,updated_at) VALUES(?,?,?,?,?)",
                    (group_id, name, description, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户组名称已存在") from exc
        return self.get_group(group_id)

    def get_group(self, group_id: str) -> Optional[AccessGroup]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM access_groups WHERE id=?", (group_id,)).fetchone()
            if not row:
                return None
            members = conn.execute(
                "SELECT username FROM access_group_members WHERE group_id=? ORDER BY username",
                (group_id,),
            ).fetchall()
        return AccessGroup(row["id"], row["name"], row["description"],
                           tuple(item["username"] for item in members),
                           row["created_at"], row["updated_at"])

    def list_groups(self) -> list[AccessGroup]:
        with self._connect() as conn:
            ids = [row["id"] for row in conn.execute("SELECT id FROM access_groups ORDER BY name")]
        return [group for group_id in ids if (group := self.get_group(group_id))]

    def delete_group(self, group_id: str) -> None:
        with self._lock, self._connect() as conn:
            if conn.execute("DELETE FROM access_groups WHERE id=?", (group_id,)).rowcount != 1:
                raise ValueError("用户组不存在")

    def add_group_member(self, group_id: str, username: str) -> None:
        username = username.strip()
        if not username or not self.get_group(group_id):
            raise ValueError("用户或用户组不存在")
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO access_group_members(group_id,username,created_at) VALUES(?,?,?)",
                (group_id, username, _now()),
            )

    def remove_group_member(self, group_id: str, username: str) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM access_group_members WHERE group_id=? AND username=?",
                         (group_id, username))

    def grant_group(self, *, group_id: str, data_source_id: str, schema_name: str,
                    table_name: str, allowed_columns: list[str], operations: list[str],
                    max_rows: int = 10000, row_filter_sql: str = "",
                    masked_columns: Optional[dict[str, str]] = None) -> AccessPolicy:
        group = self.get_group(group_id)
        if not group:
            raise ValueError("用户组不存在")
        columns = sorted({c.strip() for c in allowed_columns if c.strip()}, key=str.lower)
        ops = sorted({o.strip() for o in operations if o.strip()})
        if not schema_name.strip() or not table_name.strip() or not columns:
            raise ValueError("Schema、表和授权字段不能为空")
        if not ops or any(o not in OPERATIONS for o in ops):
            raise ValueError("操作权限无效")
        if not 1 <= int(max_rows) <= 1_000_000:
            raise ValueError("权限最大行数必须在 1～1,000,000 之间")
        masks = self._normalize_masks(masked_columns or {}, columns)
        policy_id, now = secrets.token_hex(8), _now()
        with self._lock, self._connect() as conn:
            if not conn.execute("SELECT 1 FROM data_sources WHERE id=?", (data_source_id,)).fetchone():
                raise ValueError("数据源不存在")
            conn.execute(
                """INSERT INTO group_access_policies
                   (id,group_id,data_source_id,schema_name,table_name,allowed_columns_json,
                    operations_json,max_rows,row_filter_sql,masked_columns_json,enabled,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?)
                   ON CONFLICT(group_id,data_source_id,schema_name,table_name) DO UPDATE SET
                     allowed_columns_json=excluded.allowed_columns_json,
                     operations_json=excluded.operations_json,max_rows=excluded.max_rows,
                     row_filter_sql=excluded.row_filter_sql,
                     masked_columns_json=excluded.masked_columns_json,
                     enabled=1,updated_at=excluded.updated_at""",
                (policy_id, group_id, data_source_id, schema_name.strip(), table_name.strip(),
                 json.dumps(columns, ensure_ascii=False), json.dumps(ops), int(max_rows),
                 row_filter_sql.strip(), json.dumps(masks, ensure_ascii=False), now, now),
            )
            row = conn.execute(
                """SELECT gp.*, g.name AS group_name FROM group_access_policies gp
                   JOIN access_groups g ON g.id=gp.group_id
                   WHERE gp.group_id=? AND gp.data_source_id=? AND gp.schema_name=? AND gp.table_name=?""",
                (group_id, data_source_id, schema_name.strip(), table_name.strip()),
            ).fetchone()
        return self._group_policy_row(row)

    @classmethod
    def _group_policy_row(cls, row) -> AccessPolicy:
        data = dict(row)
        data["username"] = "@" + data.get("group_name", data["group_id"])
        return cls._row(data)

    def list_group_policies(self) -> list[AccessPolicy]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT gp.*, g.name AS group_name FROM group_access_policies gp
                   JOIN access_groups g ON g.id=gp.group_id
                   ORDER BY g.name,gp.data_source_id,gp.schema_name,gp.table_name"""
            ).fetchall()
        return [self._group_policy_row(row) for row in rows]

    def revoke_group_policy(self, policy_id: str) -> None:
        with self._lock, self._connect() as conn:
            if conn.execute("DELETE FROM group_access_policies WHERE id=?", (policy_id,)).rowcount != 1:
                raise ValueError("组权限不存在")

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
            group_sql = """SELECT gp.*, g.name AS group_name FROM group_access_policies gp
                JOIN access_groups g ON g.id=gp.group_id
                JOIN access_group_members gm ON gm.group_id=g.id
                WHERE gm.username=? AND gp.enabled=1"""
            group_params: list[str] = [username]
            if data_source_id:
                group_sql += " AND gp.data_source_id=?"
                group_params.append(data_source_id)
            group_sql += " ORDER BY gp.data_source_id,gp.schema_name,gp.table_name"
            group_rows = conn.execute(group_sql, group_params).fetchall()
        return [self._row(row) for row in rows] + [self._group_policy_row(row) for row in group_rows]

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
                (audit_id, request_id, username, data_source_id, operation,
                 redact_audit_sql(sql_text),
                 json.dumps(tables, ensure_ascii=False), json.dumps(columns, ensure_ascii=False),
                 status, int(row_count), int(duration_ms), error_code[:100], _now()),
            )
        return audit_id

    def list_audits(self, limit: int = 200, *, username: str = "",
                    data_source_id: str = "", operation: str = "",
                    status: str = "") -> list[dict]:
        sql, params = "SELECT * FROM query_audits WHERE 1=1", []
        for column, value in (("username", username), ("data_source_id", data_source_id),
                              ("operation", operation), ("status", status)):
            if value:
                sql += f" AND {column}=?"
                params.append(value)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(max(limit, 1), 1000))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def audit_summary(self) -> dict:
        with self._connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM query_audits").fetchone()[0]
            success = conn.execute(
                "SELECT COUNT(*) FROM query_audits WHERE status='success'"
            ).fetchone()[0]
            denied = conn.execute(
                "SELECT COUNT(*) FROM query_audits WHERE status='denied'"
            ).fetchone()[0]
            cancelled = conn.execute(
                "SELECT COUNT(*) FROM query_audits WHERE status='cancelled'"
            ).fetchone()[0]
            timeout = conn.execute(
                "SELECT COUNT(*) FROM query_audits WHERE status='timeout'"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT COALESCE(SUM(row_count),0) FROM query_audits WHERE operation IN ('query','analyze','export') AND status='success'"
            ).fetchone()[0]
            users = conn.execute(
                "SELECT COUNT(DISTINCT username) FROM query_audits"
            ).fetchone()[0]
        return {"total": int(total), "success": int(success), "denied": int(denied),
                "cancelled": int(cancelled), "timeout": int(timeout),
                "rows": int(rows), "users": int(users)}

