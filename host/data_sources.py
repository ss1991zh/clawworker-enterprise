"""企业数据源配置与结构目录存储。

控制数据使用管理端自带的 SQLite，业务数据库仍保持远程、只读连接。数据库密码
使用 :mod:`host.secret_store` 保护后才允许入库；对外返回的 ``DataSource`` 永远不含
密码字段，避免模板、API 或日志误把凭据序列化出去。
"""
from __future__ import annotations

import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from host import secret_store


SUPPORTED_ENGINES = ("mysql", "postgresql", "sqlserver")
DEFAULT_PORTS = {"mysql": 3306, "postgresql": 5432, "sqlserver": 1433}
SSL_MODES = ("disable", "prefer", "require", "verify_ca", "verify_identity")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CredentialProtectionError(ValueError):
    """数据库密码无法安全加密。"""


@dataclass(frozen=True)
class DataSource:
    id: str
    name: str
    engine: str
    host: str
    port: int
    database_name: str
    username: str
    ssl_mode: str
    ca_path: str
    connect_timeout_seconds: int
    query_timeout_seconds: int
    max_rows: int
    max_result_bytes: int
    max_concurrent_queries: int
    max_queries_per_minute: int
    enabled: bool
    created_at: str
    updated_at: str
    last_test_status: str = "untested"
    last_test_message: str = ""
    last_test_at: str = ""
    catalog_synced_at: str = ""

    @property
    def endpoint(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True)
class CatalogColumn:
    schema_name: str
    table_name: str
    table_type: str
    column_name: str
    data_type: str
    ordinal_position: int
    nullable: bool
    comment: str = ""


class DataSourceStore:
    """线程安全的 SQLite 数据源仓库；每次操作使用独立短连接。"""

    def __init__(
        self,
        db_path: Optional[Path] = None,
        *,
        protect: Callable[[str], str] = secret_store.protect,
        unprotect: Callable[[str], str] = secret_store.unprotect,
        harden: Callable[[Path], bool] = secret_store.harden_file,
        require_encryption: Optional[bool] = None,
    ) -> None:
        self.db_path = Path(
            db_path
            or os.environ.get("CLAWWORKER_CONTROL_DB", "")
            or Path.home() / ".agent-system" / "host-data" / "control.db"
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._protect = protect
        self._unprotect = unprotect
        self._harden = harden
        self._require_encryption = os.name == "nt" if require_encryption is None else require_encryption
        self._lock = threading.RLock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS data_sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    engine TEXT NOT NULL,
                    host TEXT NOT NULL,
                    port INTEGER NOT NULL,
                    database_name TEXT NOT NULL,
                    username TEXT NOT NULL,
                    password_cipher TEXT NOT NULL,
                    ssl_mode TEXT NOT NULL DEFAULT 'prefer',
                    ca_path TEXT NOT NULL DEFAULT '',
                    connect_timeout_seconds INTEGER NOT NULL DEFAULT 8,
                    query_timeout_seconds INTEGER NOT NULL DEFAULT 60,
                    max_rows INTEGER NOT NULL DEFAULT 10000,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_test_status TEXT NOT NULL DEFAULT 'untested',
                    last_test_message TEXT NOT NULL DEFAULT '',
                    last_test_at TEXT NOT NULL DEFAULT '',
                    catalog_synced_at TEXT NOT NULL DEFAULT '',
                    CHECK (engine IN ('mysql', 'postgresql', 'sqlserver')),
                    CHECK (ssl_mode IN ('disable', 'prefer', 'require', 'verify_ca', 'verify_identity')),
                    CHECK (port BETWEEN 1 AND 65535),
                    CHECK (connect_timeout_seconds BETWEEN 1 AND 120),
                    CHECK (query_timeout_seconds BETWEEN 1 AND 3600),
                    CHECK (max_rows BETWEEN 1 AND 1000000)
                );

                CREATE TABLE IF NOT EXISTS data_catalog_columns (
                    data_source_id TEXT NOT NULL,
                    schema_name TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    table_type TEXT NOT NULL,
                    column_name TEXT NOT NULL,
                    data_type TEXT NOT NULL,
                    ordinal_position INTEGER NOT NULL,
                    nullable INTEGER NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (data_source_id, schema_name, table_name, column_name),
                    FOREIGN KEY (data_source_id) REFERENCES data_sources(id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS ix_catalog_source_table
                    ON data_catalog_columns(data_source_id, schema_name, table_name);
                """
            )
            # 1.6.1 查询执行管控。SQLite 的 CREATE TABLE IF NOT EXISTS 不会给旧库
            # 自动补列，因此逐列做兼容迁移；已有数据源直接继承安全默认值。
            existing = {row[1] for row in conn.execute("PRAGMA table_info(data_sources)")}
            migrations = {
                "max_result_bytes": "INTEGER NOT NULL DEFAULT 52428800",
                "max_concurrent_queries": "INTEGER NOT NULL DEFAULT 1",
                "max_queries_per_minute": "INTEGER NOT NULL DEFAULT 20",
            }
            for column, definition in migrations.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE data_sources ADD COLUMN {column} {definition}")
            self._harden(self.db_path)

    @staticmethod
    def _validate(
        *, name: str, engine: str, host: str, port: int, database_name: str,
        username: str, ssl_mode: str, connect_timeout_seconds: int,
        query_timeout_seconds: int, max_rows: int, max_result_bytes: int,
        max_concurrent_queries: int, max_queries_per_minute: int,
    ) -> None:
        if not name.strip():
            raise ValueError("数据源名称不能为空")
        if engine not in SUPPORTED_ENGINES:
            raise ValueError("暂只支持 MySQL、PostgreSQL 和 SQL Server")
        if not host.strip() or any(c.isspace() for c in host.strip()):
            raise ValueError("数据库主机地址不能为空且不能包含空格")
        if not 1 <= int(port) <= 65535:
            raise ValueError("数据库端口必须在 1～65535 之间")
        if not database_name.strip():
            raise ValueError("数据库名称不能为空")
        if not username.strip():
            raise ValueError("数据库用户名不能为空")
        if ssl_mode not in SSL_MODES:
            raise ValueError("TLS 模式无效")
        if not 1 <= int(connect_timeout_seconds) <= 120:
            raise ValueError("连接超时必须在 1～120 秒之间")
        if not 1 <= int(query_timeout_seconds) <= 3600:
            raise ValueError("查询超时必须在 1～3600 秒之间")
        if not 1 <= int(max_rows) <= 1_000_000:
            raise ValueError("最大返回行数必须在 1～1,000,000 之间")
        if not 1 * 1024 * 1024 <= int(max_result_bytes) <= 1024 * 1024 * 1024:
            raise ValueError("最大结果大小必须在 1～1024 MB 之间")
        if not 1 <= int(max_concurrent_queries) <= 20:
            raise ValueError("单用户并发查询数必须在 1～20 之间")
        if not 1 <= int(max_queries_per_minute) <= 600:
            raise ValueError("每分钟查询次数必须在 1～600 之间")

    def _encrypt_password(self, password: str) -> str:
        if not password:
            raise ValueError("数据库密码不能为空")
        encrypted = self._protect(password)
        if self._require_encryption and not secret_store.is_protected(encrypted):
            raise CredentialProtectionError("数据库密码加密失败，已拒绝保存；请检查 Windows DPAPI")
        return encrypted

    @staticmethod
    def _row(row: sqlite3.Row) -> DataSource:
        d = dict(row)
        d.pop("password_cipher", None)
        d["enabled"] = bool(d["enabled"])
        return DataSource(**d)

    def create(
        self, *, name: str, engine: str, host: str, port: Optional[int],
        database_name: str, username: str, password: str,
        ssl_mode: str = "prefer", ca_path: str = "",
        connect_timeout_seconds: int = 8, query_timeout_seconds: int = 60,
        max_rows: int = 10_000, max_result_bytes: int = 50 * 1024 * 1024,
        max_concurrent_queries: int = 1, max_queries_per_minute: int = 20,
    ) -> DataSource:
        engine = engine.strip().lower()
        actual_port = int(port or DEFAULT_PORTS.get(engine, 0))
        values = dict(
            name=name.strip(), engine=engine, host=host.strip(), port=actual_port,
            database_name=database_name.strip(), username=username.strip(),
            ssl_mode=ssl_mode.strip().lower(),
            connect_timeout_seconds=int(connect_timeout_seconds),
            query_timeout_seconds=int(query_timeout_seconds), max_rows=int(max_rows),
            max_result_bytes=int(max_result_bytes),
            max_concurrent_queries=int(max_concurrent_queries),
            max_queries_per_minute=int(max_queries_per_minute),
        )
        self._validate(**values)
        password_cipher = self._encrypt_password(password)
        source_id, now = secrets.token_hex(8), _now()
        try:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """INSERT INTO data_sources
                    (id,name,engine,host,port,database_name,username,password_cipher,
                     ssl_mode,ca_path,connect_timeout_seconds,query_timeout_seconds,max_rows,
                     max_result_bytes,max_concurrent_queries,max_queries_per_minute,
                     enabled,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                    (source_id, values["name"], engine, values["host"], actual_port,
                     values["database_name"], values["username"], password_cipher,
                     values["ssl_mode"], ca_path.strip(), values["connect_timeout_seconds"],
                     values["query_timeout_seconds"], values["max_rows"],
                     values["max_result_bytes"], values["max_concurrent_queries"],
                     values["max_queries_per_minute"], now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"数据源名称「{values['name']}」已存在") from exc
        return self.get(source_id)  # type: ignore[return-value]

    def update(self, source_id: str, *, password: str = "", **changes) -> DataSource:
        current = self.get(source_id)
        if not current:
            raise ValueError("数据源不存在")
        merged = {
            "name": str(changes.get("name", current.name)).strip(),
            "engine": str(changes.get("engine", current.engine)).strip().lower(),
            "host": str(changes.get("host", current.host)).strip(),
            "port": int(changes.get("port", current.port)),
            "database_name": str(changes.get("database_name", current.database_name)).strip(),
            "username": str(changes.get("username", current.username)).strip(),
            "ssl_mode": str(changes.get("ssl_mode", current.ssl_mode)).strip().lower(),
            "connect_timeout_seconds": int(changes.get("connect_timeout_seconds", current.connect_timeout_seconds)),
            "query_timeout_seconds": int(changes.get("query_timeout_seconds", current.query_timeout_seconds)),
            "max_rows": int(changes.get("max_rows", current.max_rows)),
            "max_result_bytes": int(changes.get("max_result_bytes", current.max_result_bytes)),
            "max_concurrent_queries": int(changes.get("max_concurrent_queries", current.max_concurrent_queries)),
            "max_queries_per_minute": int(changes.get("max_queries_per_minute", current.max_queries_per_minute)),
        }
        self._validate(**merged)
        fields = [
            "name=?", "engine=?", "host=?", "port=?", "database_name=?", "username=?",
            "ssl_mode=?", "ca_path=?", "connect_timeout_seconds=?",
            "query_timeout_seconds=?", "max_rows=?", "max_result_bytes=?",
            "max_concurrent_queries=?", "max_queries_per_minute=?", "updated_at=?",
            "last_test_status='untested'", "last_test_message=''", "last_test_at=''",
        ]
        params: list[object] = [
            merged["name"], merged["engine"], merged["host"], merged["port"],
            merged["database_name"], merged["username"], merged["ssl_mode"],
            str(changes.get("ca_path", current.ca_path)).strip(),
            merged["connect_timeout_seconds"], merged["query_timeout_seconds"],
            merged["max_rows"], merged["max_result_bytes"],
            merged["max_concurrent_queries"], merged["max_queries_per_minute"], _now(),
        ]
        if password:
            fields.append("password_cipher=?")
            params.append(self._encrypt_password(password))
        params.append(source_id)
        try:
            with self._lock, self._connect() as conn:
                conn.execute(f"UPDATE data_sources SET {', '.join(fields)} WHERE id=?", params)
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"数据源名称「{merged['name']}」已存在") from exc
        return self.get(source_id)  # type: ignore[return-value]

    def get(self, source_id: str) -> Optional[DataSource]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM data_sources WHERE id=?", (source_id,)).fetchone()
        return self._row(row) if row else None

    def list_all(self) -> list[DataSource]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM data_sources ORDER BY name COLLATE NOCASE").fetchall()
        return [self._row(row) for row in rows]

    def get_password(self, source_id: str) -> str:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT password_cipher FROM data_sources WHERE id=?", (source_id,)
            ).fetchone()
        if not row:
            raise ValueError("数据源不存在")
        password = self._unprotect(row["password_cipher"])
        if password == row["password_cipher"] and secret_store.is_protected(row["password_cipher"]):
            raise CredentialProtectionError("数据库密码无法解密；可能已更换 Windows 用户或安装密钥")
        return password

    def set_enabled(self, source_id: str, enabled: bool) -> DataSource:
        with self._lock, self._connect() as conn:
            cur = conn.execute(
                "UPDATE data_sources SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), _now(), source_id),
            )
            if cur.rowcount != 1:
                raise ValueError("数据源不存在")
        return self.get(source_id)  # type: ignore[return-value]

    def delete(self, source_id: str) -> None:
        with self._lock, self._connect() as conn:
            cur = conn.execute("DELETE FROM data_sources WHERE id=?", (source_id,))
            if cur.rowcount != 1:
                raise ValueError("数据源不存在")

    def record_test(self, source_id: str, ok: bool, message: str) -> None:
        safe_message = (message or ("连接成功" if ok else "连接失败"))[:500]
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE data_sources
                   SET last_test_status=?, last_test_message=?, last_test_at=?, updated_at=?
                   WHERE id=?""",
                ("ok" if ok else "failed", safe_message, _now(), _now(), source_id),
            )

    def replace_catalog(self, source_id: str, columns: Iterable[CatalogColumn]) -> int:
        items = list(columns)
        now = _now()
        with self._lock, self._connect() as conn:
            if not conn.execute("SELECT 1 FROM data_sources WHERE id=?", (source_id,)).fetchone():
                raise ValueError("数据源不存在")
            conn.execute("DELETE FROM data_catalog_columns WHERE data_source_id=?", (source_id,))
            conn.executemany(
                """INSERT INTO data_catalog_columns
                   (data_source_id,schema_name,table_name,table_type,column_name,data_type,
                    ordinal_position,nullable,comment)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (source_id, c.schema_name, c.table_name, c.table_type, c.column_name,
                     c.data_type, int(c.ordinal_position), int(c.nullable), c.comment[:1000])
                    for c in items
                ],
            )
            conn.execute(
                "UPDATE data_sources SET catalog_synced_at=?, updated_at=? WHERE id=?",
                (now, now, source_id),
            )
        return len(items)

    def catalog_summary(self, source_id: str) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*) AS columns,
                          COUNT(DISTINCT schema_name || char(0) || table_name) AS tables,
                          COUNT(DISTINCT schema_name) AS schemas
                   FROM data_catalog_columns WHERE data_source_id=?""",
                (source_id,),
            ).fetchone()
        return {"schemas": int(row["schemas"]), "tables": int(row["tables"]),
                "columns": int(row["columns"])}

    def list_catalog(self, source_id: str) -> list[CatalogColumn]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT schema_name,table_name,table_type,column_name,data_type,
                          ordinal_position,nullable,comment
                   FROM data_catalog_columns WHERE data_source_id=?
                   ORDER BY schema_name,table_name,ordinal_position""",
                (source_id,),
            ).fetchall()
        return [CatalogColumn(**{**dict(row), "nullable": bool(row["nullable"])}) for row in rows]

