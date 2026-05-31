"""
Excel 输出测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import load_workbook

from client.excel_output import SheetData, make_excel_path, write_excel
from shared.contract import ChartSpec


def test_make_excel_path_in_downloads(tmp_downloads: Path):
    p = make_excel_path()
    assert str(p).startswith(str(tmp_downloads))
    assert p.name.startswith("analysis_")
    assert p.suffix == ".xlsx"


def test_write_excel_basic(tmp_downloads: Path):
    sheets = [
        SheetData(
            name="MonthlyTrend",
            headers=["month", "amount_sum"],
            rows=[["2024-01", 300], ["2024-02", 400]],
            chart=ChartSpec(type="line", x="month", y="amount_sum"),
        )
    ]
    result = write_excel(sheets)
    assert result.path.exists()
    assert result.charts_generated == 1
    assert result.sheet_names == ["MonthlyTrend"]

    wb = load_workbook(result.path)
    ws = wb["MonthlyTrend"]
    assert ws["A1"].value == "month"
    assert ws["B1"].value == "amount_sum"
    assert ws["A2"].value == "2024-01"
    assert ws["B3"].value == 400


def test_write_excel_refuses_path_outside_downloads(tmp_path: Path, tmp_downloads: Path):
    sheets = [SheetData(name="X", headers=["a"], rows=[[1]])]
    bad_path = tmp_path / "elsewhere" / "x.xlsx"
    bad_path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(PermissionError):
        write_excel(sheets, path=bad_path)


def test_write_excel_does_not_overwrite(tmp_downloads: Path):
    sheets = [SheetData(name="X", headers=["a"], rows=[[1]])]
    p = tmp_downloads / "fixed.xlsx"
    write_excel(sheets, path=p)
    # 再次写同名,应该报错(严守"每次新文件")
    with pytest.raises(FileExistsError):
        write_excel(sheets, path=p)


def test_write_excel_multi_sheet(tmp_downloads: Path):
    sheets = [
        SheetData(name="S1", headers=["x"], rows=[[1], [2]]),
        SheetData(name="S2", headers=["y"], rows=[[3]]),
    ]
    result = write_excel(sheets)
    assert result.sheet_names == ["S1", "S2"]
    wb = load_workbook(result.path)
    assert set(wb.sheetnames) == {"S1", "S2"}
