"""自然语言到候选 SQL：模型只看授权结构，结果必须再次经过查询网关。"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass


class NaturalQueryError(ValueError):
    pass


@dataclass(frozen=True)
class NaturalQueryCandidate:
    sql: str
    explanation: str


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
6. 不执行查询，不声称已经查到结果。"""
    user = (f"数据库类型：{engine}\n数据源显示名：{source_name}\n\n"
            "授权结构：\n" + "\n".join(lines) +
            f"\n\n用户需求：{intent.strip()}")
    return system, user


def generate_candidate(provider, *, engine: str, source_name: str, intent: str,
                       catalog: list[dict]) -> NaturalQueryCandidate:
    system, user = build_schema_prompt(
        engine=engine, source_name=source_name, intent=intent, catalog=catalog,
    )
    try:
        raw = provider.raw_chat(system=system, user=user)
    except Exception as exc:
        raise NaturalQueryError(f"模型生成查询失败：{type(exc).__name__}") from exc
    value = _extract_json(raw)
    sql = str(value.get("sql") or "").strip()
    explanation = str(value.get("explanation") or "").strip()
    if not sql:
        raise NaturalQueryError(explanation or "当前授权结构无法满足这个查询需求")
    if len(sql) > 20000:
        raise NaturalQueryError("模型生成的 SQL 过长")
    return NaturalQueryCandidate(sql=sql, explanation=explanation or "已生成候选查询")
