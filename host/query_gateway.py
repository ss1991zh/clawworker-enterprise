"""SQL AST 安全校验、权限核对与只读执行。"""
from __future__ import annotations

import secrets
import json
import threading
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


class QueryCancelled(RuntimeError):
    pass


class QueryTimedOut(RuntimeError):
    pass


class ResultTooLarge(RuntimeError):
    pass


class QueryExecutionSignal:
    """线程安全的查询中止信号；任务管理器可在驱动执行期间调用 cancel。"""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._cancel_driver = None
        self.reason = ""

    def bind(self, callback) -> None:
        with self._lock:
            self._cancel_driver = callback
            already_cancelled = self._event.is_set()
        if already_cancelled:
            self._invoke(callback)

    def clear_driver(self) -> None:
        with self._lock:
            self._cancel_driver = None

    def cancel(self, reason: str = "cancelled") -> None:
        with self._lock:
            if not self._event.is_set():
                self.reason = reason
                self._event.set()
            callback = self._cancel_driver
        if callback:
            self._invoke(callback)

    @staticmethod
    def _invoke(callback) -> None:
        try:
            callback()
        except Exception:
            pass

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            if self.reason == "timeout":
                raise QueryTimedOut("查询超过管理员设置的时间限制")
            raise QueryCancelled("查询已由用户取消")


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
        table_policies: dict[str, list[AccessPolicy]] = {}
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
            table_policies[table.alias_or_name.lower()] = matches
        if not tables:
            raise QueryDenied("查询必须访问至少一张已授权业务表", "no_table")

        aliases = {t.alias_or_name.lower(): t.alias_or_name.lower() for t in tree.find_all(exp.Table)
                   if t.name.lower() not in cte_names}
        requested_columns: list[str] = []
        column_bindings: list[tuple[object, list[str]]] = []
        required_by_alias: dict[str, set[str]] = {alias: set() for alias in table_policies}
        for col in tree.find_all(exp.Column):
            name = col.name
            table_alias = aliases.get((col.table or "").lower(), "")
            if col.table and not table_alias and col.table.lower() not in cte_names:
                raise QueryDenied(f"字段限定符 {col.table} 未对应已授权表", "column_denied")
            candidate_aliases = [table_alias] if table_alias else list(table_policies)
            candidate_sets = [table_policies[alias] for alias in candidate_aliases]
            # 未限定表名时，只有所有被查询物理表都授权该列才放行。这样不会因数据库
            # 自己解析同名列而把某张表上的未授权字段带出来。
            allowed = bool(candidate_sets) and all(
                any(p.permits_column(name) for p in policy_set) for policy_set in candidate_sets
            )
            if not allowed:
                raise QueryDenied(f"无权访问字段 {name}", "column_denied")
            for alias in candidate_aliases:
                required_by_alias[alias].add(name)
            column_bindings.append((col, candidate_aliases))
            requested_columns.append(name)

        # 防止跨授权拼字段扩大权限：每张表参与本次查询的全部字段，必须由至少一条
        # 个人或组授权完整覆盖。比如 A 组只给 id、B 组只给 salary，不能拼成
        # SELECT id,salary 后再把两组的行范围合并。
        scoped_policies: dict[str, list[AccessPolicy]] = {}
        for alias, policies_for_table in table_policies.items():
            required = required_by_alias[alias]
            scoped = [p for p in policies_for_table
                      if all(p.permits_column(column) for column in required)]
            if not scoped:
                raise QueryDenied(
                    "所选字段分别来自不同授权范围，不能在同一查询中组合",
                    "column_scope_conflict",
                )
            scoped_policies[alias] = scoped
        table_policies = scoped_policies

        masked_nodes: list[tuple[object, str]] = []
        for col, candidate_aliases in column_bindings:
            mask = self._effective_mask(
                [table_policies[alias] for alias in candidate_aliases], col.name,
            )
            if mask:
                if not self._is_direct_projection(col, exp):
                    raise QueryDenied(
                        f"脱敏字段 {col.name} 只能直接出现在结果列，不能用于筛选、连接、排序或计算",
                        "masked_column_usage",
                    )
                masked_nodes.append((col, mask))

        # 多条个人/组授权是权限并集；每张物理表取可用授权中最高行数，再由整个数据源
        # 上限和多表中最小上限共同约束。
        table_limits = [max(p.max_rows for p in policy_set)
                        for policy_set in table_policies.values()]
        max_rows = min(source.max_rows, *table_limits)

        self._apply_row_filters(tree, exp, source, table_policies)
        for column, strategy in masked_nodes:
            self._apply_mask(column, strategy, source, exp)
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

    @staticmethod
    def _effective_mask(candidate_sets: list[list[AccessPolicy]], column: str) -> str:
        """每张候选表分别合并权限；任一授权明确不脱敏即可看原值。"""
        rank = {"": 0, "partial": 1, "hash": 2, "null": 3}
        effective = []
        for policies in candidate_sets:
            applicable = [p for p in policies if p.permits_column(column)]
            if not applicable:
                continue
            masks = [p.mask_for(column) for p in applicable]
            if "" in masks:
                effective.append("")
            else:
                effective.append(max(masks, key=lambda item: rank.get(item, 3)))
        if not effective or all(not item for item in effective):
            return ""
        return max((item for item in effective if item), key=lambda item: rank.get(item, 3))

    @staticmethod
    def _is_direct_projection(column, exp) -> bool:
        parent = column.parent
        if isinstance(parent, exp.Alias):
            parent = parent.parent
        return isinstance(parent, exp.Select) and any(
            item is column or (isinstance(item, exp.Alias) and item.this is column)
            for item in parent.expressions
        )

    def _parse_policy_condition(self, source: DataSource, text: str, exp):
        try:
            import sqlglot
            condition = sqlglot.parse_one(
                text, read=self.DIALECTS[source.engine], into=exp.Condition,
            )
        except Exception as exc:
            raise QueryDenied(f"管理员配置的行级策略无效：{safe_error(exc)}",
                              "invalid_row_policy") from exc
        allowed = (exp.Paren, exp.And, exp.Or, exp.Not, exp.EQ, exp.NEQ,
                   exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between,
                   exp.Is, exp.Like, exp.Column, exp.Identifier, exp.Literal,
                   exp.Null, exp.Boolean, exp.Neg, exp.Tuple)
        if any(not isinstance(node, allowed) for node in condition.walk()):
            raise QueryDenied(
                "行级策略只允许列、常量、比较、IN/BETWEEN、IS NULL 和 AND/OR，不允许函数或子查询",
                "invalid_row_policy",
            )
        for column in condition.find_all(exp.Column):
            if column.table:
                raise QueryDenied("行级策略字段不能自行指定表名", "invalid_row_policy")
        return condition

    def _apply_row_filters(self, tree, exp, source: DataSource,
                           table_policies: dict[str, list[AccessPolicy]]) -> None:
        filters: dict[str, str] = {}
        for alias, policies in table_policies.items():
            values = [p.row_filter_sql.strip() for p in policies]
            # 空条件代表这条授权对该表不限制行；多条受限授权则按范围并集 OR。
            if any(not value for value in values):
                continue
            filters[alias] = " OR ".join(f"({value})" for value in values if value)
        if not filters:
            return

        applied: set[str] = set()
        for select in tree.find_all(exp.Select):
            conditions = []
            for table in select.find_all(exp.Table):
                if table.name.lower() in {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}:
                    continue
                if table.find_ancestor(exp.Select) is not select:
                    continue
                alias = table.alias_or_name.lower()
                if alias not in filters:
                    continue
                condition = self._parse_policy_condition(source, filters[alias], exp)
                # 策略使用真实列名；查询有别名时补上别名，避免多表同名列歧义。
                for column in condition.find_all(exp.Column):
                    if not column.table:
                        column.set("table", exp.to_identifier(table.alias_or_name))
                conditions.append(condition)
                applied.add(alias)
            for condition in conditions:
                select.where(condition, append=True, copy=False)
        missing = set(filters) - applied
        if missing:
            raise QueryDenied("无法安全地将行级策略应用到复杂查询", "row_policy_not_applied")

    def _apply_mask(self, column, strategy: str, source: DataSource, exp) -> None:
        rendered = column.sql(dialect=self.DIALECTS[source.engine])
        if strategy == "null":
            expression = exp.Null()
        else:
            if source.engine == "mysql":
                sql = (f"CONCAT(LEFT(CAST({rendered} AS CHAR), 2), '***', "
                       f"RIGHT(CAST({rendered} AS CHAR), 2))" if strategy == "partial" else
                       f"SHA2(CAST({rendered} AS CHAR), 256)")
            elif source.engine == "postgresql":
                sql = (f"CONCAT(LEFT(CAST({rendered} AS TEXT), 2), '***', "
                       f"RIGHT(CAST({rendered} AS TEXT), 2))" if strategy == "partial" else
                       f"MD5(CAST({rendered} AS TEXT))")
            else:
                sql = (f"CONCAT(LEFT(CAST({rendered} AS NVARCHAR(MAX)), 2), '***', "
                       f"RIGHT(CAST({rendered} AS NVARCHAR(MAX)), 2))" if strategy == "partial" else
                       f"CONVERT(VARCHAR(64), HASHBYTES('SHA2_256', "
                       f"CONVERT(NVARCHAR(MAX), {rendered})), 2)")
            try:
                import sqlglot
                expression = sqlglot.parse_one(sql, read=self.DIALECTS[source.engine])
            except Exception as exc:
                raise QueryDenied("无法应用字段脱敏策略", "invalid_mask_policy") from exc
        parent = column.parent
        if isinstance(parent, exp.Alias):
            parent.set("this", expression)
        else:
            column.replace(exp.alias_(expression, column.name, quoted=False))

    def preview(self, **kwargs) -> QueryPlan:
        return self.plan(**kwargs)

    def validate_row_filter(self, *, data_source_id: str, row_filter_sql: str,
                            catalog_columns: set[str]) -> None:
        """保存授权前验证行级条件；执行时仍会重新验证，防止绕过管理页。"""
        if not row_filter_sql.strip():
            return
        source = self.sources.get(data_source_id)
        if not source:
            raise QueryDenied("数据源不存在", "source_unavailable")
        _, exp = self._parse(source, "SELECT 1 FROM policy_validation")
        condition = self._parse_policy_condition(source, row_filter_sql.strip(), exp)
        unknown = sorted({column.name for column in condition.find_all(exp.Column)
                          if column.name.lower() not in catalog_columns})
        if unknown:
            raise QueryDenied(f"行级条件包含不存在的字段：{', '.join(unknown)}",
                              "invalid_row_policy")

    def execute(self, *, username: str, data_source_id: str, sql: str,
                operation: str = "query", request_id: str = "",
                signal: QueryExecutionSignal | None = None) -> dict:
        request_id = request_id or secrets.token_hex(10)
        started = time.monotonic()
        plan = None
        signal = signal or QueryExecutionSignal()
        try:
            signal.raise_if_cancelled()
            plan = self.plan(username=username, data_source_id=data_source_id,
                             sql=sql, operation=operation)
            source = self.sources.get(data_source_id)
            password = self.sources.get_password(data_source_id)
            columns, rows, result_bytes = self._execute(source, password, plan, signal)
            signal.raise_if_cancelled()
            duration = int((time.monotonic() - started) * 1000)
            self.access.record_audit(
                request_id=request_id, username=username, data_source_id=data_source_id,
                operation=operation, sql_text=plan.sql, tables=list(plan.tables),
                columns=list(plan.columns), status="success", row_count=len(rows),
                duration_ms=duration,
            )
            return {"request_id": request_id, "columns": columns, "rows": rows,
                    "row_count": len(rows), "truncated_at": plan.max_rows,
                    "result_bytes": result_bytes, "duration_ms": duration}
        except QueryCancelled:
            self.access.record_audit(
                request_id=request_id, username=username, data_source_id=data_source_id,
                operation=operation, sql_text=(plan.sql if plan else sql),
                tables=list(plan.tables) if plan else [], columns=list(plan.columns) if plan else [],
                status="cancelled", duration_ms=int((time.monotonic()-started)*1000),
                error_code="cancelled",
            )
            raise
        except QueryTimedOut:
            self.access.record_audit(
                request_id=request_id, username=username, data_source_id=data_source_id,
                operation=operation, sql_text=(plan.sql if plan else sql),
                tables=list(plan.tables) if plan else [], columns=list(plan.columns) if plan else [],
                status="timeout", duration_ms=int((time.monotonic()-started)*1000),
                error_code="query_timeout",
            )
            raise
        except ResultTooLarge:
            self.access.record_audit(
                request_id=request_id, username=username, data_source_id=data_source_id,
                operation=operation, sql_text=(plan.sql if plan else sql),
                tables=list(plan.tables) if plan else [], columns=list(plan.columns) if plan else [],
                status="denied", duration_ms=int((time.monotonic()-started)*1000),
                error_code="result_too_large",
            )
            raise
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

    def _execute(self, source: DataSource, password: str, plan: QueryPlan,
                 signal: QueryExecutionSignal):
        connector = self.connectors.for_engine(source.engine)
        with closing(connector._connect(source, password)) as conn:
            cur = conn.cursor()
            try:
                cancel_driver = getattr(cur, "cancel", None)
                if not callable(cancel_driver):
                    cancel_driver = getattr(conn, "cancel", None)
                if not callable(cancel_driver):
                    cancel_driver = conn.close
                signal.bind(cancel_driver)
                if source.engine == "mysql":
                    cur.execute("SET SESSION TRANSACTION READ ONLY")
                    cur.execute(f"SET SESSION MAX_EXECUTION_TIME={source.query_timeout_seconds * 1000}")
                elif source.engine == "postgresql":
                    cur.execute("SET default_transaction_read_only = on")
                    cur.execute("SET statement_timeout = %s", (source.query_timeout_seconds * 1000,))
                elif source.engine == "sqlserver":
                    cur.execute(f"SET LOCK_TIMEOUT {source.query_timeout_seconds * 1000}")
                    try:
                        cur.timeout = source.query_timeout_seconds
                    except Exception:
                        pass
                signal.raise_if_cancelled()
                cur.execute(plan.sql)
                signal.raise_if_cancelled()
                headers = [str(d[0]) for d in (cur.description or [])]
                result_bytes = len(json.dumps(headers, ensure_ascii=False).encode("utf-8"))
                rows = []
                while len(rows) < plan.max_rows:
                    signal.raise_if_cancelled()
                    batch = cur.fetchmany(min(256, plan.max_rows - len(rows)))
                    if not batch:
                        break
                    for raw in batch:
                        row = list(raw)
                        result_bytes += len(json.dumps(
                            row, ensure_ascii=False, default=str,
                        ).encode("utf-8"))
                        if result_bytes > source.max_result_bytes:
                            raise ResultTooLarge(
                                f"查询结果超过管理员设置的 {source.max_result_bytes // 1048576} MB 限制"
                            )
                        rows.append(row)
                return headers, rows, result_bytes
            except (QueryCancelled, QueryTimedOut, ResultTooLarge):
                raise
            except Exception as exc:
                signal.raise_if_cancelled()
                text = safe_error(exc).lower()
                if any(marker in text for marker in (
                    "timeout", "timed out", "statement timeout", "maximum statement execution",
                    "query timeout", "hyt00", "hyt01",
                )):
                    raise QueryTimedOut("查询超过管理员设置的时间限制") from exc
                raise
            finally:
                signal.clear_driver()
                try:
                    cur.close()
                except Exception:
                    pass
