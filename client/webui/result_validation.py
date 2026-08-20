"""分析结果验收阶段：指标列、有效值和部分成功说明。"""
from __future__ import annotations

import math
import numbers
import re

from shared.analysis_requirements import extract_analysis_requirements


def required_output_metrics(user_query: str) -> list[str]:
    return extract_analysis_requirements(user_query).labels


def metric_column_matches(metric: str, column: str) -> bool:
    target = re.sub(r"[\s（）()_\-]+", "", metric)
    normalized = re.sub(r"[\s（）()_\-]+", "", column)
    if target not in normalized:
        return False
    if target in {"收入", "毛利"}:
        derived_markers = ("同比", "环比", "增长", "率", "占比", "目标", "预算", "上年", "去年")
        return not any(marker in normalized for marker in derived_markers)
    return True


def series_has_valid_value(series) -> bool:
    try:
        values = series.dropna().tolist()
    except Exception:
        return False
    for value in values:
        if isinstance(value, numbers.Number):
            try:
                if math.isfinite(float(value)):
                    return True
            except (TypeError, ValueError, OverflowError):
                continue
        elif str(value).strip() and str(value).strip().lower() not in {"nan", "none", "null"}:
            return True
    return False


def missing_required_metrics(results: list, required_metrics: list[str]) -> list[str]:
    columns: list[tuple[str, object]] = []
    for result in results or []:
        frame = result.get("df") if isinstance(result, dict) else None
        for column in getattr(frame, "columns", []):
            normalized = re.sub(r"[\s（）()_\-]+", "", str(column))
            if normalized:
                columns.append((normalized, frame[column]))
    missing = []
    for metric in required_metrics:
        matches = [series for column, series in columns if metric_column_matches(metric, column)]
        if not matches or not any(series_has_valid_value(series) for series in matches):
            missing.append(metric)
    return missing


def summary_acknowledges_metric_skip(summary: str, metric: str) -> bool:
    target = re.sub(r"[\s（）()_\-]+", "", metric)
    compact = re.sub(r"[\s（）()_\-]+", "", summary or "")
    if target not in compact:
        return False
    skip_words = ("跳过", "无法计算", "不能计算", "不可计算", "缺少", "不足", "未提供", "无有效")
    for match in re.finditer(re.escape(target), compact):
        context = compact[max(0, match.start() - 80):match.end() + 80]
        if any(word in context for word in skip_words):
            return True
    return False


def drop_empty_metric_columns(results: list, skipped_metrics: list[str]) -> None:
    for result in results or []:
        frame = result.get("df") if isinstance(result, dict) else None
        if frame is None:
            continue
        drop = []
        for column in getattr(frame, "columns", []):
            if any(metric_column_matches(metric, str(column)) for metric in skipped_metrics):
                if not series_has_valid_value(frame[column]):
                    drop.append(column)
        if drop:
            result["df"] = frame.drop(columns=drop)


def append_skipped_metrics_summary(summary: str, skipped_metrics: list[str]) -> str:
    if not skipped_metrics:
        return summary
    names = "、".join(dict.fromkeys(skipped_metrics))
    notice = (
        f"部分指标已跳过：{names}。现有授权数据缺少必要计算参数或有效值，"
        "且无法由其他字段可靠地直接或间接推导；其余可计算任务已继续完成。"
    )
    return f"{summary.rstrip()}\n\n{notice}" if summary.strip() else notice
