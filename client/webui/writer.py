"""
v2 Excel writer — 把 [(sheet_name, DataFrame, chart_hint)] 直接写成多 sheet xlsx。

不依赖 LangGraph / workflow / ops。每个 skill 函数产出的 (name, df, chart)
被一一渲染:
- 数字列自动格式(率→0.00% / 金额→#,##0.00 / 天数→0.0 / 其他→0)
- 如果 chart_hint 含 type/x/y 就加柱状图或折线图
- 输出落到 ~/Downloads/analysis_<ts>.xlsx
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


# 路径白名单(B6 §2)
def make_excel_path(stem: Optional[str] = None) -> Path:
    """生成 ~/Downloads/<stem>_<ts>.xlsx 路径,stem 不传默认 "analysis"。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_stem = (stem or "analysis").strip("_") or "analysis"
    return Path.home() / "Downloads" / f"{safe_stem}_{ts}.xlsx"


# ----------------------------------------------------------------------------
# 文件名衍生:<密文文件名> + <用户需求关键词> + <时间戳>
# ----------------------------------------------------------------------------

# 用户问题里要去掉的"动词/语气词前缀",防止文件名一上来就是「计算...」
_QUERY_VERBS = (
    "计算", "统计", "分析", "汇总", "排名", "排行", "列出", "生成", "导出",
    "看一下", "看下", "看看", "看", "算一下", "算下", "算出", "算", "求一下", "求",
    "查询", "查一下", "查", "做一下", "做下", "做",
    "帮我", "帮忙", "请", "我要", "我想", "我要看", "给我", "麻烦",
)
# 文件名非法字符 / 影响阅读的标点
_BAD_FNAME_CHARS = re.compile(r"[\\/:*?\"<>|\s,.，。;；·!?？“”‘’\[\]\(\)（）【】]+")


def _clean_cipher_stem(cipher_path: Path) -> str:
    """从密文文件路径里抠出"原文件名":去掉 _enc 后缀 / .cipher / 各种扩展。"""
    stem = cipher_path.stem  # 销售提成模拟数据_管理会计_enc
    # 去掉常见加密尾缀(_enc / _encrypted / .cipher 子串)
    for suffix in ("_enc", "_encrypted", ".cipher", "_encrypt"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    # 再次去 stem 上的后缀(.xlsx.cipher 这种)
    while Path(stem).suffix:
        stem = Path(stem).stem
    return stem.strip("_") or "data"


def _clean_query_keyword(user_query: str, max_len: int = 14) -> str:
    """从用户问题里抽一段短关键词作为文件名一部分。"""
    q = (user_query or "").strip()
    if not q:
        return ""
    # 去掉前缀动词
    for v in _QUERY_VERBS:
        if q.startswith(v):
            q = q[len(v):].strip()
            break
    # 去标点 / 空白
    q = _BAD_FNAME_CHARS.sub("", q)
    # 截断到 max_len 字符
    if len(q) > max_len:
        q = q[:max_len]
    return q.strip("_")


def derive_excel_stem(cipher_path: Optional[Path], user_query: str) -> str:
    """
    返回 Excel 文件名的 stem(无 _时间戳 部分)。
    形如:销售提成模拟数据_管理会计_销售提成
    """
    parts: list[str] = []
    if cipher_path is not None:
        cipher_stem = _clean_cipher_stem(cipher_path)
        if cipher_stem:
            parts.append(cipher_stem)
    kw = _clean_query_keyword(user_query)
    if kw:
        parts.append(kw)
    if not parts:
        return "analysis"
    stem = "_".join(parts)
    # 文件系统兜底:不要太长(Windows / macOS 都有 255 字节文件名上限)
    if len(stem) > 80:
        stem = stem[:80].rstrip("_")
    return stem


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
    stem: Optional[str] = None,
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

    dst = enforce_excel_path(path or make_excel_path(stem))
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


def export_skill_results_encrypted(
    results: list[dict],
    cipher_path: Path,
    note: str = "",
    stem: Optional[str] = None,
) -> Path:
    """
    用户选择「保留密文」时的输出 —— 与解密版同结构,但数值列再加密:
      - 多 sheet,每个 SkillCall 输出一张
      - 列布局同明文版(身份列在前,聚合 / 派生列在后)
      - 身份列(字符串)保持明文;数值列经 ct.encrypt_excel 再加密成 base64 cipher
      - 第一张「说明」sheet 列出已完成的 skill → sheet 映射
      - 无 chart(密文不可作图)
    """
    import openpyxl
    import pandas as pd
    import tempfile
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    # 初始化 HE runtime,获取 ct(用与密文文件同一套 sk/evk)
    from client.tools.runtime import Runtime
    Runtime.get().ensure_all_initialized()
    import crypto_toolkit as ct  # noqa: F401

    dst = enforce_excel_path(make_excel_path(stem))
    dst.parent.mkdir(parents=True, exist_ok=True)

    wb = openpyxl.Workbook()
    default = wb.active
    wb.remove(default)

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="2563EB")
    center = Alignment(horizontal="center", vertical="center")

    # ---- 说明 sheet ----
    notice = wb.create_sheet(title="说明")
    notice["A1"] = "结果保留密文 · 未授权解密展示"
    notice["A1"].font = Font(bold=True, size=13, color="9A3412")
    notice["A1"].fill = PatternFill("solid", fgColor="FFEDD5")
    notice["A1"].alignment = Alignment(horizontal="left", vertical="center")
    lines = [
        "",
        "密态计算已在本机完成 · 全程未暴露明文。",
        "下方各 sheet 与解密版结构一致,但数值列保持同态密文(base64 ciphertext)形式。",
        "明文身份列(姓名 / 大区 / 月份 等)仍可见,便于核对结构。",
        f"源密文文件:{cipher_path.name}",
        "",
        "已完成的 skill → 结果 sheet:",
    ]
    for r in results:
        df = r.get("df")
        if df is None or df.empty:
            continue
        skill = r.get("skill", "?")
        nm = r.get("sheet_name", "?")
        lines.append(f"  · {skill} → 「{nm}」 ({len(df)} 行 × {len(df.columns)} 列)")
    if note:
        lines.extend(["", note])
    for i, t in enumerate(lines, 2):
        notice.cell(i, 1, t)
    notice.column_dimensions["A"].width = 80

    # ---- 每张结果 sheet ----
    for r in results:
        sheet_name = str(r.get("sheet_name") or "Sheet")[:31]
        df = r.get("df")
        if df is None or df.empty:
            continue

        enc_df = _encrypt_numeric_columns(df)

        ws = wb.create_sheet(title=sheet_name)
        headers = [str(c) for c in enc_df.columns]
        for ci, h in enumerate(headers, 1):
            cell = ws.cell(1, ci, h)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = center
        for ri, row in enumerate(enc_df.itertuples(index=False), 2):
            for ci, val in enumerate(row, 1):
                ws.cell(ri, ci, val if val is not None else "")
        for ci, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(ci)].width = min(max(len(h) * 2.1, 12), 36)
        ws.freeze_panes = "A2"

    wb.save(dst)
    return dst


def _encrypt_numeric_columns(df):
    """
    对一张 skill 结果 df 做"再加密":
      身份列(非数值,如 string / category / object)保持明文原样;
      数值列(int / float)单独走 ct.encrypt_excel 文件管线 →
        读回来时数值单元格变成 base64 cipher 字符串。
      最终把两类列按原始列顺序拼回去,保持与解密版一致的布局。
    失败兜底:数值列填充 "[已加密]" 占位字符串,身份列原样保留。
    """
    import pandas as pd
    import tempfile

    try:
        from client.tools.runtime import Runtime
        Runtime.get().ensure_all_initialized()
        import crypto_toolkit as ct  # noqa: F401
    except Exception:
        ct = None

    original_cols = list(df.columns)
    df = df.copy().reset_index(drop=True)
    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if not numeric_cols or ct is None:
        return df

    # 身份列(原样保留明文)
    id_cols = [c for c in original_cols if c not in numeric_cols]
    id_df = df[id_cols] if id_cols else None

    # 数值列单独写盘 → 加密 → 读回
    num_df = df[numeric_cols].copy().fillna(0)

    tmp_plain = Path(tempfile.mkstemp(suffix=".xlsx")[1])
    tmp_enc = Path(tempfile.mkstemp(suffix=".xlsx")[1])
    enc_num_df = None
    try:
        num_df.to_excel(str(tmp_plain), index=False)
        try:
            ct.encrypt_excel(str(tmp_plain), str(tmp_enc), input_index_col=None)
            # 加密 xlsx 里现在全是字符串(base64 cipher),用 dtype=str 读避免 pandas 自动 cast
            enc_num_df = pd.read_excel(str(tmp_enc), dtype=str)
        except Exception:
            enc_num_df = None
    finally:
        tmp_plain.unlink(missing_ok=True)
        tmp_enc.unlink(missing_ok=True)

    if enc_num_df is None:
        # 加密失败兜底
        enc_num_df = pd.DataFrame({c: ["[已加密]"] * len(df) for c in numeric_cols})

    # encrypt_excel 可能在末尾补 cipher metadata 行 —— 裁回原行数
    enc_num_df = enc_num_df.reset_index(drop=True).iloc[: len(df)].copy()

    # 数值列各 cell 已是 cipher(浮点的科学计数 / 或 NaN);为了 Excel 里可读,
    # 一律 toString 之后写入,且不能再被 Excel 当数字渲染。
    for c in enc_num_df.columns:
        enc_num_df[c] = enc_num_df[c].apply(
            lambda v: "" if pd.isna(v) else (v if isinstance(v, str) else f"{v:.16e}")
        )

    # 拼回去(按原始顺序)
    if id_df is not None:
        merged = pd.concat([id_df.reset_index(drop=True), enc_num_df], axis=1)
    else:
        merged = enc_num_df
    merged = merged[[c for c in original_cols if c in merged.columns]]
    return merged


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
