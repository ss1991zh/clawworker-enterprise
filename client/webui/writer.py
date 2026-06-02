"""
v2 Excel writer — 把 [(sheet_name, DataFrame, chart_hint)] 直接写成多 sheet xlsx。

不依赖 LangGraph / workflow / ops。每个 skill 函数产出的 (name, df, chart)
被一一渲染:
- 数字列自动格式(率→0.00% / 金额→#,##0.00 / 天数→0.0 / 其他→0)
- 如果 chart_hint 含 type/x/y 就加柱状图或折线图
- 输出落到 ~/Downloads/analysis_<ts>.xlsx
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# 路径白名单(B6 §2)
def make_excel_path() -> Path:
    """生成 ~/Downloads/analysis_<ts>.xlsx 路径。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return Path.home() / "Downloads" / f"analysis_{ts}.xlsx"


def enforce_excel_path(path: Path) -> Path:
    """B6 §2:Excel 输出强制 ~/Downloads/。"""
    resolved = path.expanduser().resolve()
    downloads = (Path.home() / "Downloads").resolve()
    try:
        resolved.relative_to(downloads)
    except ValueError:
        raise PermissionError(f"Excel 输出路径不在白名单内:{resolved} 不在 {downloads}/ 下")
    return resolved


# ----------------------------------------------------------------------------
# Number format 推断
# ----------------------------------------------------------------------------

def _infer_number_format(col_name: str) -> Optional[str]:
    name = str(col_name)
    lower = name.lower()
    # 百分比
    if any(k in lower for k in ("rate", "ratio", "percentage")):
        return "0.00%"
    if any(k in name for k in ("率", "比例", "占比")):
        return "0.00%"
    # 金额
    if any(k in lower for k in ("amount", "value", "price", "cost", "revenue", "profit")):
        return "#,##0.00"
    if any(k in name for k in ("金额", "(元)", "（元）", "成本", "收入")):
        return "#,##0.00"
    # 天数
    if "days" in lower or "天数" in name or "周转" in name:
        return "0.0"
    # 整数 / 计数
    if any(k in lower for k in ("count", "num", "qty")):
        return "0"
    if any(k in name for k in ("订单数", "数量", "笔数", "次数", "排名")):
        return "0"
    return None


# ----------------------------------------------------------------------------
# 主入口
# ----------------------------------------------------------------------------

def write_skill_results(
    results: list[dict],
    path: Optional[Path] = None,
) -> Path:
    """
    把 [{sheet_name, df, chart}] 列表写成多 sheet xlsx。

    Args:
        results: 每个 dict 至少含 sheet_name + df,可选 chart(dict 或 None)
        path: 落地路径(默认 ~/Downloads/analysis_<ts>.xlsx)

    Returns:
        最终落盘路径
    """
    import openpyxl
    from openpyxl.utils import get_column_letter
    from openpyxl.chart import BarChart, LineChart, Reference
    from openpyxl.styles import Alignment, Font, PatternFill

    dst = enforce_excel_path(path or make_excel_path())
    dst.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    # 删默认 sheet
    default = wb.active
    wb.remove(default)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2563EB")
    center = Alignment(horizontal="center", vertical="center")

    for r in results:
        sheet_name = str(r.get("sheet_name") or "Sheet")[:31]
        df = r.get("df")
        chart_hint = r.get("chart")
        if df is None or df.empty:
            continue

        ws = wb.create_sheet(title=sheet_name)

        # 写表头
        headers = [str(c) for c in df.columns]
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(1, ci, h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center

        # 写数据 + 每列推断 number_format
        col_formats: dict[int, str] = {}
        for ci, col in enumerate(headers, 1):
            nf = _infer_number_format(col)
            if nf:
                col_formats[ci] = nf

        for ri, row in enumerate(df.itertuples(index=False), 2):
            for ci, val in enumerate(row, 1):
                cell = ws.cell(ri, ci, val)
                if ci in col_formats:
                    cell.number_format = col_formats[ci]

        # 列宽自动(简化:按表头长度 + 2)
        for ci, h in enumerate(headers, 1):
            width = max(len(h) * 2.1, 10) + 2
            ws.column_dimensions[get_column_letter(ci)].width = min(width, 28)

        # 冻结首行
        ws.freeze_panes = "A2"

        # 图表(可选)
        if chart_hint:
            try:
                _add_chart(ws, df, chart_hint, headers)
            except Exception:
                pass  # 图表渲染失败不阻断 Excel 落盘

    wb.save(dst)
    return dst


def _add_chart(ws, df, chart_hint: dict, headers: list[str]) -> None:
    """根据 chart_hint = {type, x, y, title} 加一个柱状/折线图。"""
    from openpyxl.chart import BarChart, LineChart, Reference

    typ = (chart_hint.get("type") or "bar").lower()
    x = chart_hint.get("x")
    y = chart_hint.get("y")
    title = chart_hint.get("title") or ""

    if not (x and y):
        return
    if isinstance(y, str):
        y_cols = [y]
    else:
        y_cols = list(y)

    if x not in headers:
        return
    valid_y = [c for c in y_cols if c in headers]
    if not valid_y:
        return

    x_col_idx = headers.index(x) + 1
    n_rows = len(df) + 1  # +1 因为表头

    chart_cls = LineChart if typ == "line" else BarChart
    chart = chart_cls()
    chart.title = title
    chart.style = 11
    chart.height = 10
    chart.width = 18

    cats = Reference(ws, min_col=x_col_idx, min_row=2, max_row=n_rows)
    chart.set_categories(cats)

    for yc in valid_y:
        y_col_idx = headers.index(yc) + 1
        data = Reference(ws, min_col=y_col_idx, min_row=1, max_row=n_rows)
        chart.add_data(data, titles_from_data=True)

    # 放在最后一列右侧
    anchor_col = chr(ord("A") + len(headers) + 1)
    ws.add_chart(chart, f"{anchor_col}2")
