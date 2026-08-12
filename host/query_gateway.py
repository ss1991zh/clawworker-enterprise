"""SQL AST 安全校验、权限核对与只读执行。"""
from __future__ import annotations

import secrets
import time
from contextlib import closing
from dataclasses import dataclass

from host.data_access import AccessPolicy, DataAccessStore
from host.data_sources import DataSource, DataSourceStore
from host.db_connectors import ConnectorRegistry, safe_error


class QueryDenied(ValueError):
    def __init__(self, message: str, code: str = "query_denied"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class QueryPlan:
    sql: str
    tables: tuple[str, ...]
    columns: tuple[str, ...]
    max_rows: int


class QueryGateway:
    DIALECTS = {"mysql": "mysql", "postgresql": "postgres", "sqlserver": "tsql"}

    def __init__(self, sources: DataSourceStore, access: DataAccessStore,
                 connectors: ConnectorRegistry):
        self.sources, self.access, self.connectors = sources, access, connectors

    def _parse(self, source: DataSource, sql: str):
        try:
            import sqlglot
            from sqlglot import exp
        except ImportError as exc:
            raise QueryDenied("缺少 SQL 安全解析器，请修复管理端安装", "parser_missing") from exc
        if not sql.strip() or len(sql) > 20000:
            raise QueryDenied("SQL为空或过长")
        try:
            statements = sqlglot.parse(sql, read=self.DIALECTS[source.engine])
        except Exception as exc:
            raise QueryDenied(f"SQL语法无法解析：{safe_error(exc)}", "syntax_error") from exc
        if len(statements) != 1:
            raise QueryDenied("一次只允许执行一条查询语句", "multiple_statements")
        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            raise QueryDenied("只允许 SELECT 或只读 CTE 查询", "not_select")
        forbidden = (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop, exp.Alter,
                     exp.Command, exp.Copy, exp.Transaction, exp.Merge)
        if any(tree.find(node) is not None for node in forbidden):
            raise QueryDenied("查询包含写入、管理或文件操作", "unsafe_statement")
        if tree.find(exp.Star) is not None:
            raise QueryDenied("第一版禁止 SELECT *，请明确列出需要的字段", "star_denied")
        rendered_lower = tree.sql(dialect=self.DIALECTS[source.engine]).lower()
        dangerous_tokens = (
            " into outfile", " into dumpfile", "load_file(", "pg_read_file(",
            "pg_read_binary_file(", "openrowset(", "opendatasource(", "xp_cmdshell",
        )
        if any(token in rendered_lower for token in dangerous_tokens):
            raise QueryDenied("查询包含文件、外部数据源或系统命令功能", "dangerous_function")
        return tree, exp

    def plan(self, *, username: str, data_source_id: str, sql: str,
             operation: str = "query") -> QueryPlan:
        source = self.sources.get(data_source_id)
        if not source or not source.enabled:
            raise QueryDenied("数据源不存在或已停用", "source_unavailable")
        tree, exp = self._parse(source, sql)
        policies = self.access.list_for_user(username, data_source_id)
        permitted = [p for p in policies if p.permits(operation)]
        if not permitted:
            raise QueryDenied("当前用户没有此数据源的查询权限", "no_source_permission")

        cte_names = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
        tables = []
        for table in tree.find_all(exp.Table):
            if table.name.lower() in cte_names:
                continue
            name = table.name.lower()
            same_name = [p for p in permitted if p.table_name.lower() == name]
            if table.db:
                matches = [p for p in same_name if p.schema_name.lower() == table.db.lower()]
            else:
                distinct_schemas = {p.schema_name.lower() for p in same_name}
                if len(distinct_schemas) > 1:
                    raise QueryDenied(
                        f"表 {table.name} 在多个已授权 Schema 中同名，请明确写 Schema",
                        "ambiguous_schema",
                    )
                matches = same_name
            if not matches:
                raise QueryDenied(f"无权访问表 {table.db + '.' if table.db else ''}{table.name}",
                                  "table_denied")
            tables.append(f"{table.db + '.' if table.db else ''}{table.name}")
        if not tables:
            raise QueryDenied("查询必须访问至少一张已授权业务表", "no_table")

        aliases = {t.alias_or_name.lower(): t.name.lower() for t in tree.find_all(exp.Table)
                   if t.name.lower() not in cte_names}
        physical_table_names = {t.name.lower() for t in tree.find_all(exp.Table)
                                if t.name.lower() not in cte_names}
        requested_columns: list[str] = []
        for col in tree.find_all(exp.Column):
            name = col.name
            table_name = aliases.get((col.table or "").lower(), "")
            if col.table and not table_name and col.table.lower() not in cte_names:
                raise QueryDenied(f"字段限定符 {col.table} 未对应已授权表", "column_denied")
            candidates = [p for p in permitted if p.table_name.lower() in physical_table_names
                          and (not table_name or p.table_name.lower() == table_name)]
            # 未限定表名时，只有所有被查询物理表都授权该列才放行。这样不会因数据库
            # 自己解析同名列而把某张表上的未授权字段带出来。
            allowed = (all(p.permits_column(name) for p in candidates)
                       if not table_name else any(p.permits_column(name) for p in candidates))
            if not candidates or not allowed:
                raise QueryDenied(f"无权访问字段 {name}", "column_denied")
            requested_columns.append(name)

        max_rows = min(source.max_rows, *(p.max_rows for p in permitted))
        existing_limit = tree.args.get("limit")
        if existing_limit:
            try:
                requested = int(existing_limit.expression.name)
                max_rows = min(max_rows, requested)
            except Exception:
                raise QueryDenied("LIMIT/TOP 必须是固定整数", "dynamic_limit")
        tree.set("limit", exp.Limit(expression=exp.Literal.number(max_rows)))
        rendered = tree.sql(dialect=self.DIALECTS[source.engine])
        return QueryPlan(rendered, tuple(dict.fromkeys(tables)),
                         tuple(dict.fromkeys(requested_columns)), max_rows)

    def preview(self, **kwargs) -> QueryPlan:
        return self.plan(**kwargs)

    def execute(self, *, username: str, data_source_id: str, sql: str,
                operation: str = "query") -> dict:
        request_id = secrets.token_hex(10)
        started = time.monotonic()
        plan = None
        try:
            plan = self.plan(username=username, data_source_id=data_source_id,
                             sql=sql, operation=operation)
            source = self.sources.get(data_source_id)
            password = self.sources.get_password(data_source_id)
            columns, rows = self._execute(source, password, plan)
            duration = int((time.monotonic() - started) * 1000)
            self.access.record_audit(
                request_id=request_id, username=username, data_source_id=data_source_id,
                operation=operation, sql_text=plan.sql, tables=list(plan.tables),
                columns=list(plan.columns), status="success", row_count=len(rows),
                duration_ms=duration,
            )
            return {"request_id": request_id, "columns": columns, "rows": rows,
                    "row_count": len(rows), "truncated_at": plan.max_rows,
                    "duration_ms": duration}
        except QueryDenied as exc:
            self.access.record_audit(
                request_id=request_id, username=username, data_source_id=data_source_id,
                operation=operation, sql_text=(plan.sql if plan else sql),
                tables=list(plan.tables) if plan else [], columns=list(plan.columns) if plan else [],
                status="denied", duration_ms=int((time.monotonic()-started)*1000),
                error_code=exc.code,
            )
            raise
        except Exception as exc:
            self.access.record_audit(
                request_id=request_id, username=username, data_source_id=data_source_id,
                operation=operation, sql_text=(plan.sql if plan else sql),
                tables=list(plan.tables) if plan else [], columns=list(plan.columns) if plan else [],
                status="failed", duration_ms=int((time.monotonic()-started)*1000),
                error_code=type(exc).__name__,
            )
            raise RuntimeError(safe_error(exc)) from exc

    def _execute(self, source: DataSource, password: str, plan: QueryPlan):
        connector = self.connectors.for_engine(source.engine)
        with closing(connector._connect(source, password)) as conn:
            cur = conn.cursor()
            try:
                if source.engine == "mysql":
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={source.query_timeout_seconds * 1000}")
                elif source.engine == "postgresql":
                    cur.execute("SET default_transaction_read_only = on")
                    cur.execute("SET statement_timeout = %s", (source.query_timeout_seconds * 1000,))
                elif source.engine == "sqlserver":
                    cur.execute(f"SET LOCK_TIMEOUT {source.query_timeout_seconds * 1000}")
                cur.execute(plan.sql)
                headers = [str(d[0]) for d in (cur.description or [])]
                rows = [list(row) for row in cur.fetchmany(plan.max_rows)]
                return headers, rows
            finally:
                cur.close()
