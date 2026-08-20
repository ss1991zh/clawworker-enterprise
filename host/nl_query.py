"""自然语言到候选 SQL：模型只看授权结构，结果必须再次经过查询网关。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from shared.analysis_requirements import (
    assess_catalog,
    catalog_assessment_text,
    extract_analysis_requirements,
)


class NaturalQueryError(ValueError):
    pass


@dataclass(frozen=True)
class NaturalQueryCandidate:
    sql: str
    explanation: str


def _deterministic_financial_yoy_candidate(*, engine: str, intent: str,
                                           catalog: list[dict]) -> NaturalQueryCandidate | None:
    """常用月度收入/毛利同比走固定只读取数，避免模型生成脆弱 SQL。

    数据库只返回已授权的月份、收入和毛利基础列；同比在用户端完成本地加密后，
    由受控计算环境按前 12 期计算。结构不完全匹配时返回 None，继续通用规划。
    """
    requirements = extract_analysis_requirements(intent)
    labels = set(requirements.labels)
    if engine.lower() != "mysql" or not any("同比" in label for label in labels):
        return None
    need_revenue = any(label == "收入" or label.startswith("收入同比") for label in labels)
    need_profit = any(label == "毛利" or label.startswith("毛利同比") for label in labels)
    # 固定读取方案只覆盖已经用真实数据库验证过的“收入 + 毛利 + 同比”组合；
    # 单指标同比仍走通用规划和日期连接修复，保留其数据库侧同比语义。
    if not (need_revenue and need_profit):
        return None
    time_names = ("month_start",)
    revenue_names = ("net_revenue", "revenue", "sales_revenue", "sales_amount")
    profit_names = ("gross_profit",)
    cost_names = ("standard_cost", "cost_amount", "sales_cost", "cost_of_goods_sold", "cogs")
    best: tuple[int, dict, list[str]] | None = None
    for table in catalog:
        by_lower = {
            str(column.get("name") or "").lower(): str(column.get("name") or "")
            for column in table.get("columns", [])
        }
        pick = lambda names: next((by_lower[name] for name in names if name in by_lower), "")
        month = pick(time_names)
        revenue = pick(revenue_names)
        profit = pick(profit_names)
        cost = pick(cost_names)
        revenue_ready = bool(revenue)
        profit_ready = bool(profit or (revenue and cost))
        if not month or not ((need_revenue and revenue_ready) or (need_profit and profit_ready)):
            continue
        selected = [month]
        requested_columns = (
            revenue if need_revenue or (need_profit and not profit) else "",
            profit if need_profit else "",
            cost if need_profit and revenue and not profit else "",
        )
        for column in requested_columns:
            if column and column not in selected:
                selected.append(column)
        score = int(need_revenue and revenue_ready) * 2 + int(need_profit and profit_ready) * 2
        if best is None or score > best[0]:
            best = (score, table, selected)

    if best is not None:
        _, table, selected = best
        schema = str(table.get("schema") or "")
        table_name = str(table.get("table") or "")
        if not schema or not table_name:
            return None

        def quote(identifier: str) -> str:
            return "`" + identifier.replace("`", "``") + "`"

        sql = (
            f"SELECT {', '.join(quote(column) for column in selected)} "
            f"FROM {quote(schema)}.{quote(table_name)} ORDER BY {quote(selected[0])}"
        )
        return NaturalQueryCandidate(
            sql=sql,
            explanation=("读取授权月度基础数据，由用户端本机加密后计算同比；直接字段优先，"
                         "缺少直接毛利时允许由收入减成本间接计算；仍缺参数的指标说明后跳过"),
        )
    return None


def _parse_candidate(text: str) -> NaturalQueryCandidate:
    value = _extract_json(text)
    sql = str(value.get("sql") or "").strip()
    explanation = str(value.get("explanation") or "").strip()
    if not sql:
        raise NaturalQueryError(explanation or "当前授权结构无法满足这个查询需求")
    if len(sql) > 20000:
        raise NaturalQueryError("模型生成的 SQL 过长")
    return NaturalQueryCandidate(sql=sql, explanation=explanation or "已生成候选查询")


def _fragile_mysql_yoy_join(sql: str, *, engine: str, intent: str,
                            catalog: list[dict]) -> bool:
    """识别给完整日期再次拼接“日”的同比连接；这类 SQL 会成功但匹配全空。"""
    if engine.lower() != "mysql" or "同比" not in intent:
        return False
    types_by_name: dict[str, set[str]] = {}
    for table in catalog:
        for column in table.get("columns", []):
            name = str(column.get("name") or "").lower()
            if name:
                types_by_name.setdefault(name, set()).add(
                    str(column.get("type") or "").lower()
                )
    for name, data_types in types_by_name.items():
        complete_date = any(
            any(token in data_type for token in ("date", "time", "year"))
            or bool(re.search(r"(?:var)?char\s*\(\s*(?:1[0-9]|[2-9][0-9])\s*\)", data_type))
            for data_type in data_types
        )
        if complete_date and re.search(
            rf"CONCAT\s*\(\s*(?:`?\w+`?\s*\.\s*)?`?{re.escape(name)}`?\s*,",
            sql,
            re.I,
        ):
            return True
    return False


def _repair_mysql_yoy_join(sql: str, *, catalog: list[dict]) -> str:
    """将已知的字符串化同比日期连接收敛为 DATE_SUB(date_col, INTERVAL 1 YEAR)。

    只改写 JOIN ON 中的等式，且复杂一侧必须引用授权结构中明确声明为日期类型的
    字段；其他表达式原样保留。无法确定时返回空字符串，由调用方继续阻止执行。
    """
    types_by_name: dict[str, set[str]] = {}
    for table in catalog:
        for column in table.get("columns", []):
            name = str(column.get("name") or "").lower()
            if name:
                types_by_name.setdefault(name, set()).add(
                    str(column.get("type") or "").lower()
                )
    if not types_by_name:
        return ""
    try:
        import sqlglot
        from sqlglot import exp
        tree = sqlglot.parse_one(sql, read="mysql")
    except Exception:
        return ""

    changed = False
    for join in tree.find_all(exp.Join):
        condition = join.args.get("on")
        if condition is None:
            continue
        for equality in condition.find_all(exp.EQ):
            for simple, complex_expr in (
                (equality.this, equality.expression),
                (equality.expression, equality.this),
            ):
                if not isinstance(simple, exp.Column):
                    continue
                rendered = complex_expr.sql(dialect="mysql").upper()
                if "DATE_SUB(" not in rendered or not (
                    "STR_TO_DATE(" in rendered or "DATE_FORMAT(" in rendered
                ):
                    continue
                date_refs = [
                    column for column in complex_expr.find_all(exp.Column)
                    if column.name.lower() in types_by_name
                ]
                if len(date_refs) != 1 or simple.name.lower() != date_refs[0].name.lower():
                    continue
                base = date_refs[0].sql(dialect="mysql")
                data_types = types_by_name[date_refs[0].name.lower()]
                native_date = any(
                    any(token in data_type for token in ("date", "time", "year"))
                    for data_type in data_types
                )
                iso_date_text = any(
                    re.search(r"(?:var)?char\s*\(\s*(?:1[0-9]|[2-9][0-9])\s*\)", data_type)
                    for data_type in data_types
                )
                if not native_date and not iso_date_text:
                    continue
                if native_date:
                    fixed_expression = f"DATE_SUB({base}, INTERVAL 1 YEAR)"
                else:
                    fixed_expression = (
                        "DATE_FORMAT(DATE_SUB(STR_TO_DATE("
                        f"{base}, '%Y-%m-%d'), INTERVAL 1 YEAR), '%Y-%m-%d')"
                    )
                try:
                    replacement = sqlglot.parse_one(
                        fixed_expression, read="mysql",
                    )
                except Exception:
                    continue
                complex_expr.replace(replacement)
                changed = True
                break
    return tree.sql(dialect="mysql") if changed else ""


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.I | re.S)
    candidate = fenced.group(1) if fenced else text
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise NaturalQueryError("模型没有返回可识别的查询方案")
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise NaturalQueryError("模型返回的查询方案格式无效") from exc
    if not isinstance(value, dict):
        raise NaturalQueryError("模型返回的查询方案格式无效")
    return value


def build_schema_prompt(*, engine: str, source_name: str, intent: str,
                        catalog: list[dict]) -> tuple[str, str]:
    if not intent.strip() or len(intent.strip()) > 2000:
        raise NaturalQueryError("查询需求不能为空或超过 2000 字")
    lines = []
    for table in catalog:
        columns = []
        for column in table.get("columns", []):
            mask = column.get("mask") or ""
            suffix = f" [结果将{mask}脱敏]" if mask else ""
            columns.append(f"{column['name']} {column.get('type', '')}{suffix}")
        lines.append(f"- {table['schema']}.{table['table']}: " + ", ".join(columns))
    if not lines:
        raise NaturalQueryError("没有可用于规划的授权表结构")
    system = """你是企业数据库只读查询规划器。只根据给出的授权结构生成一条查询候选。
必须遵守：
1. 只返回 JSON：{"sql":"...","explanation":"..."}，不要 Markdown。
2. 只能生成单条 SELECT；禁止 SELECT *、写入、DDL、存储过程、系统表、文件和外部数据源。
3. 每个字段都显式写出；表名使用 schema.table；需要聚合时使用明确别名。
4. 不猜测不存在的表或字段；需求无法由结构满足时，sql 返回空字符串并说明原因。
5. 不在 SQL 中自行添加权限、行级范围或脱敏表达式，这些由安全网关统一处理。
6. 不执行查询，不声称已经查到结果。
7. 用户要求同比时必须真正取得去年同期值并计算增长率。MySQL 的 DATE/DATETIME 字段直接使用 DATE_SUB(当前日期字段, INTERVAL 1 YEAR)；varchar(10) 的 YYYY-MM-DD 文本日期用 STR_TO_DATE 后再减一年，绝不能再 CONCAT '-01'；只有 varchar(7) 的 YYYY-MM 才可补 '-01'。连续月度序列也可按时间排序后用 LAG(..., 12)。
8. 同比结果应显式输出本期值、去年同期值和同比增长率，分母为 0 或去年同期缺失时才返回 NULL。
9. 先把用户需求拆成独立指标，逐项检查授权字段：优先直接字段，其次检查能否由其他字段按可靠公式间接推导。某个指标缺少必要参数且无法推导时，不得让整条查询失败；SQL 继续提取其余可计算指标所需的基础字段，并在 explanation 中明确列出跳过的指标及缺少的参数。只有所有指标都无法满足时才返回空 sql。
10. 数据库查询以提取基础字段为主，派生指标可留给用户端本机加密后的受控计算环境完成，禁止为了一个派生指标生成不必要的复杂子查询。"""
    requirements = extract_analysis_requirements(intent)
    assessment = catalog_assessment_text(assess_catalog(requirements, catalog))
    assessment_block = (
        f"\n\n系统预检查（只依据授权字段名和可靠公式）：\n{assessment}"
        if assessment else ""
    )
    user = (f"数据库类型：{engine}\n数据源显示名：{source_name}\n\n"
            "授权结构：\n" + "\n".join(lines) + assessment_block +
            f"\n\n用户需求：{intent.strip()}")
    return system, user


def generate_candidate(provider, *, engine: str, source_name: str, intent: str,
                       catalog: list[dict]) -> NaturalQueryCandidate:
    deterministic = _deterministic_financial_yoy_candidate(
        engine=engine, intent=intent, catalog=catalog,
    )
    if deterministic is not None:
        return deterministic
    system, user = build_schema_prompt(
        engine=engine, source_name=source_name, intent=intent, catalog=catalog,
    )
    try:
        raw = provider.raw_chat(system=system, user=user)
    except Exception as exc:
        raise NaturalQueryError(f"模型生成查询失败：{type(exc).__name__}") from exc
    candidate = _parse_candidate(raw)
    if _fragile_mysql_yoy_join(
        candidate.sql, engine=engine, intent=intent, catalog=catalog,
    ):
        correction = (
            system
            + "\n9. 上一个候选给完整日期再次拼接了日，可能成功执行但上年同期会全空。"
              "请根据字段真实类型重新生成：DATE 字段直接 DATE_SUB；varchar(10) 的 "
              "YYYY-MM-DD 先 STR_TO_DATE 但不要 CONCAT；也可对连续月度数据用 LAG(..., 12)。"
        )
        try:
            raw = provider.raw_chat(system=correction, user=user)
        except Exception as exc:
            raise NaturalQueryError(f"模型修正同比查询失败：{type(exc).__name__}") from exc
        candidate = _parse_candidate(raw)
        if _fragile_mysql_yoy_join(
            candidate.sql, engine=engine, intent=intent, catalog=catalog,
        ):
            repaired_sql = _repair_mysql_yoy_join(candidate.sql, catalog=catalog)
            if not repaired_sql or _fragile_mysql_yoy_join(
                repaired_sql, engine=engine, intent=intent, catalog=catalog,
            ):
                raise NaturalQueryError("同比查询的日期连接不可靠，已阻止执行，请重试")
            candidate = NaturalQueryCandidate(
                sql=repaired_sql,
                explanation=f"{candidate.explanation}；已自动修正同比日期连接",
            )
    return candidate
