"""远程业务数据库只读连接器。

驱动均按需导入，使缺少某一种驱动时只影响该类数据源。所有公开错误都会经过
``safe_error`` 清洗，严禁把密码或完整连接串写入管理端页面与日志。
"""
from __future__ import annotations

import re
from contextlib import closing
from dataclasses import dataclass
from typing import Callable, Protocol

from host.data_sources import CatalogColumn, DataSource, SUPPORTED_ENGINES


class ConnectorUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class ConnectionTestResult:
    ok: bool
    message: str
    server_version: str = ""


class Connector(Protocol):
    def test(self, source: DataSource, password: str) -> ConnectionTestResult: ...
    def inspect_catalog(self, source: DataSource, password: str) -> list[CatalogColumn]: ...


def safe_error(exc: BaseException, password: str = "") -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if password:
        text = text.replace(password, "***")
    text = re.sub(r"(?i)(password|pwd)\s*=\s*[^;\s]+", r"\1=***", text)
    return (text or type(exc).__name__)[:500]


def _mysql_ssl(source: DataSource):
    if source.ssl_mode == "disable":
        return None
    ssl: dict[str, object] = {}
    if source.ca_path:
        ssl["ca"] = source.ca_path
    if source.ssl_mode in ("verify_ca", "verify_identity"):
        ssl["check_hostname"] = source.ssl_mode == "verify_identity"
    return ssl


class MySQLConnector:
    def _connect(self, source: DataSource, password: str):
        try:
            import pymysql
        except ImportError as exc:
            raise ConnectorUnavailable("缺少 MySQL 驱动 PyMySQL，请重新安装或修复管理端") from exc
        kwargs = dict(
            host=source.host, port=source.port, user=source.username, password=password,
            database=source.database_name, connect_timeout=source.connect_timeout_seconds,
            read_timeout=source.query_timeout_seconds, write_timeout=source.query_timeout_seconds,
            charset="utf8mb4", autocommit=True,
        )
        ssl = _mysql_ssl(source)
        if ssl is not None:
            kwargs["ssl"] = ssl
        return pymysql.connect(**kwargs)

    def test(self, source: DataSource, password: str) -> ConnectionTestResult:
        with closing(self._connect(source, password)) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT VERSION(), @@transaction_read_only")
                version, read_only = cur.fetchone()
        mode = "只读" if bool(read_only) else "账号可写（Clawworker仍会强制只读）"
        return ConnectionTestResult(True, f"连接成功 · {mode}", str(version))

    def inspect_catalog(self, source: DataSource, password: str) -> list[CatalogColumn]:
        sql = """
            SELECT c.TABLE_SCHEMA, c.TABLE_NAME, t.TABLE_TYPE, c.COLUMN_NAME,
                   c.COLUMN_TYPE, c.ORDINAL_POSITION, c.IS_NULLABLE, c.COLUMN_COMMENT
            FROM information_schema.COLUMNS c
            JOIN information_schema.TABLES t
              ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME
            WHERE c.TABLE_SCHEMA=%s AND t.TABLE_TYPE IN ('BASE TABLE','VIEW')
            ORDER BY c.TABLE_SCHEMA,c.TABLE_NAME,c.ORDINAL_POSITION
        """
        with closing(self._connect(source, password)) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (source.database_name,))
                rows = cur.fetchall()
        return [
            CatalogColumn(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]),
                          int(r[5]), str(r[6]).upper() == "YES", str(r[7] or ""))
            for r in rows
        ]


class PostgreSQLConnector:
    def _connect(self, source: DataSource, password: str):
        try:
            import psycopg
        except ImportError as exc:
            raise ConnectorUnavailable("缺少 PostgreSQL 驱动 psycopg，请重新安装或修复管理端") from exc
        sslmode = {"verify_ca": "verify-ca", "verify_identity": "verify-full"}.get(
            source.ssl_mode, source.ssl_mode
        )
        kwargs = dict(host=source.host, port=source.port, dbname=source.database_name,
                      user=source.username, password=password,
                      connect_timeout=source.connect_timeout_seconds, sslmode=sslmode)
        if source.ca_path:
            kwargs["sslrootcert"] = source.ca_path
        conn = psycopg.connect(**kwargs)
        conn.autocommit = True
        return conn

    def test(self, source: DataSource, password: str) -> ConnectionTestResult:
        with self._connect(source, password) as conn, conn.cursor() as cur:
            cur.execute("SET statement_timeout = %s", (source.query_timeout_seconds * 1000,))
            cur.execute("SELECT version(), current_setting('transaction_read_only')")
            version, read_only = cur.fetchone()
        mode = "只读" if str(read_only).lower() == "on" else "账号可写（Clawworker仍会强制只读）"
        return ConnectionTestResult(True, f"连接成功 · {mode}", str(version).split(",")[0])

    def inspect_catalog(self, source: DataSource, password: str) -> list[CatalogColumn]:
        sql = """
            SELECT c.table_schema,c.table_name,t.table_type,c.column_name,c.data_type,
                   c.ordinal_position,c.is_nullable,COALESCE(d.description,'')
            FROM information_schema.columns c
            JOIN information_schema.tables t USING (table_schema,table_name)
            LEFT JOIN pg_catalog.pg_class pc ON pc.relname=c.table_name
            LEFT JOIN pg_catalog.pg_namespace pn ON pn.oid=pc.relnamespace AND pn.nspname=c.table_schema
            LEFT JOIN pg_catalog.pg_description d ON d.objoid=pc.oid AND d.objsubid=c.ordinal_position
            WHERE c.table_schema NOT IN ('pg_catalog','information_schema')
              AND t.table_type IN ('BASE TABLE','VIEW')
            ORDER BY c.table_schema,c.table_name,c.ordinal_position
        """
        with self._connect(source, password) as conn, conn.cursor() as cur:
            cur.execute("SET statement_timeout = %s", (source.query_timeout_seconds * 1000,))
            cur.execute(sql)
            rows = cur.fetchall()
        return [CatalogColumn(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]),
                              int(r[5]), str(r[6]).upper() == "YES", str(r[7] or ""))
                for r in rows]


class SQLServerConnector:
    def _connect(self, source: DataSource, password: str):
        try:
            import pyodbc
        except ImportError as exc:
            raise ConnectorUnavailable("缺少 SQL Server 驱动 pyodbc，请重新安装或修复管理端") from exc
        if "ODBC Driver 18 for SQL Server" not in pyodbc.drivers():
            raise ConnectorUnavailable("缺少 Microsoft ODBC Driver 18 for SQL Server，请修复管理端安装")
        encrypt = "no" if source.ssl_mode == "disable" else "yes"
        trust = "yes" if source.ssl_mode in ("prefer", "require") else "no"
        parts = [
            "DRIVER={ODBC Driver 18 for SQL Server}", f"SERVER={source.host},{source.port}",
            f"DATABASE={source.database_name}", f"UID={source.username}", f"PWD={password}",
            f"Encrypt={encrypt}", f"TrustServerCertificate={trust}",
            f"Connection Timeout={source.connect_timeout_seconds}",
            "ApplicationIntent=ReadOnly",
        ]
        return pyodbc.connect(";".join(parts), autocommit=True,
                              timeout=source.query_timeout_seconds)

    def test(self, source: DataSource, password: str) -> ConnectionTestResult:
        with closing(self._connect(source, password)) as conn:
            cur = conn.cursor()
            cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(128))")
            version = cur.fetchone()[0]
            cur.close()
        return ConnectionTestResult(True, "连接成功 · ApplicationIntent=ReadOnly", str(version))

    def inspect_catalog(self, source: DataSource, password: str) -> list[CatalogColumn]:
        sql = """
            SELECT c.TABLE_SCHEMA,c.TABLE_NAME,t.TABLE_TYPE,c.COLUMN_NAME,c.DATA_TYPE,
                   c.ORDINAL_POSITION,c.IS_NULLABLE,COALESCE(ep.value,'')
            FROM INFORMATION_SCHEMA.COLUMNS c
            JOIN INFORMATION_SCHEMA.TABLES t
              ON t.TABLE_SCHEMA=c.TABLE_SCHEMA AND t.TABLE_NAME=c.TABLE_NAME
            LEFT JOIN sys.schemas s ON s.name=c.TABLE_SCHEMA
            LEFT JOIN sys.tables st ON st.name=c.TABLE_NAME AND st.schema_id=s.schema_id
            LEFT JOIN sys.columns sc ON sc.object_id=st.object_id AND sc.name=c.COLUMN_NAME
            LEFT JOIN sys.extended_properties ep ON ep.major_id=st.object_id
              AND ep.minor_id=sc.column_id AND ep.name='MS_Description'
            WHERE t.TABLE_TYPE IN ('BASE TABLE','VIEW')
            ORDER BY c.TABLE_SCHEMA,c.TABLE_NAME,c.ORDINAL_POSITION
        """
        with closing(self._connect(source, password)) as conn:
            cur = conn.cursor()
            cur.execute(sql)
            rows = cur.fetchall()
            cur.close()
        return [CatalogColumn(str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]),
                              int(r[5]), str(r[6]).upper() == "YES", str(r[7] or ""))
                for r in rows]


class ConnectorRegistry:
    def __init__(self) -> None:
        self._connectors: dict[str, Connector] = {
            "mysql": MySQLConnector(), "postgresql": PostgreSQLConnector(),
            "sqlserver": SQLServerConnector(),
        }

    def for_engine(self, engine: str) -> Connector:
        try:
            return self._connectors[engine]
        except KeyError as exc:
            raise ValueError(f"不支持的数据库类型：{engine}") from exc

    def test(self, source: DataSource, password: str) -> ConnectionTestResult:
        try:
            return self.for_engine(source.engine).test(source, password)
        except Exception as exc:
            return ConnectionTestResult(False, safe_error(exc, password))

    def inspect_catalog(self, source: DataSource, password: str) -> list[CatalogColumn]:
        return self.for_engine(source.engine).inspect_catalog(source, password)

    @property
    def engines(self) -> tuple[str, ...]:
        return SUPPORTED_ENGINES
