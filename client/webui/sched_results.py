"""
密态定时任务的结果暂存 / 批量解密。

定时密态任务到点正常计算,但结果**不解密**,而是加密暂存到沙盒;
用户回来按任务批量解密 → 所有结果明文 Excel 落到一个文件夹。

每张结果 df 拆两部分持久化(沙盒 ~/.agent-system/scheduler/enc_results/):
  - 数值列  → ct.encrypt_excel 暂存为可再解密的密文 xlsx
  - 身份列  → 明文 csv sidecar(姓名 / 大区 等,本系统一贯按明文 metadata 处理)
  - manifest 记录 sheet 名 + 两个文件路径 + 列顺序

批量解密:ps.read_excel + ct.decrypt_df 还原数值列 → 拼回身份列 → 多 sheet 明文 Excel。
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any, Optional


ENC_RESULTS_DIR = Path.home() / ".agent-system" / "scheduler" / "enc_results"


def persist_results_encrypted(results: list[dict], run_id: str) -> list[dict]:
    """
    把一次运行的 results([{sheet_name, df, chart}])加密暂存到沙盒。
    返回 manifest 列表:[{sheet_name, num_enc, id_csv, col_order, n_rows}]
    """
    import pandas as pd
    import tempfile

    from client.tools.runtime import Runtime
    Runtime.get().ensure_all_initialized()
    import crypto_toolkit as ct  # noqa: F401

    run_dir = ENC_RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    manifest: list[dict] = []
    for idx, r in enumerate(results):
        df = r.get("df")
        if df is None or getattr(df, "empty", True):
            continue
        df = df.copy().reset_index(drop=True)
        col_order = [str(c) for c in df.columns]
        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        id_cols = [c for c in df.columns if c not in numeric_cols]

        num_enc_path = ""
        if numeric_cols:
            num_df = df[numeric_cols].copy().fillna(0)
            # 用 with 立即关闭句柄,避免 Windows 上 mkstemp 泄漏 fd 占用文件(WinError 32)
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as _f:
                tmp_plain = Path(_f.name)
            enc_path = run_dir / f"sheet{idx}_num.xlsx"
            try:
                num_df.to_excel(str(tmp_plain), index=False)
                ct.encrypt_excel(str(tmp_plain), str(enc_path), input_index_col=None)
                num_enc_path = str(enc_path)
            finally:
                tmp_plain.unlink(missing_ok=True)

        id_csv_path = ""
        if id_cols:
            id_df = df[id_cols].copy()
            p = run_dir / f"sheet{idx}_id.csv"
            id_df.to_csv(str(p), index=False)
            id_csv_path = str(p)

        entry = {
            "sheet_name": str(r.get("sheet_name") or f"结果{idx+1}")[:31],
            "num_enc": num_enc_path,
            "id_csv": id_csv_path,
            "numeric_cols": [str(c) for c in numeric_cols],
            "col_order": col_order,
            "n_rows": int(len(df)),
        }
        # 呈现键(chart/档位/合计行等)一并暂存 —— 批量解密时按产品级渲染还原,
        # 保证定时任务出的 Excel 和单独提问完全一个样
        for k in ("chart", "charts", "tier_col", "total_row", "note", "number_formats"):
            v = r.get(k)
            if v:
                try:
                    json.dumps(v)   # 只存可序列化的
                    entry[k] = v
                except Exception:
                    pass
        manifest.append(entry)

    # manifest 也落一份(方便排查)
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _sanitize(name: str, fallback: str = "定时任务结果") -> str:
    return "".join(ch for ch in (name or "") if ch not in '\\/:*?"<>|').strip() or fallback


def task_output_subdirs(output_folder: str) -> tuple[Path, Path]:
    """在每任务的输出文件夹下确保 密文/ 与 明文/ 两个子文件夹存在,返回 (密文, 明文)。"""
    root = Path(output_folder).expanduser()
    cipher_dir = root / "密文"
    plain_dir = root / "明文"
    cipher_dir.mkdir(parents=True, exist_ok=True)
    plain_dir.mkdir(parents=True, exist_ok=True)
    return cipher_dir, plain_dir


def export_encrypted_run_to_folder(run_id: str, manifest: list, output_folder: str,
                                   stem: str, run_at: str = "") -> Optional[Path]:
    """把一次运行的加密结果(沙盒密文 + 明文身份列)组装成一个**密文 Excel**,
    落到 <output_folder>/密文/。数值列保持 base64 密文,身份列明文 —— 未授权也能看到/留存。
    返回落盘路径(无可写内容则 None)。"""
    import pandas as pd
    from datetime import datetime

    from client.webui.writer import write_skill_results

    cipher_dir, _ = task_output_subdirs(output_folder)
    sheets: list[dict] = []
    for entry in manifest:
        parts = []
        if entry.get("id_csv") and Path(entry["id_csv"]).exists():
            parts.append(pd.read_csv(entry["id_csv"], dtype=str, keep_default_na=False)
                         .reset_index(drop=True))
        if entry.get("num_enc") and Path(entry["num_enc"]).exists():
            # 密文 xlsx:数值列已是 base64 ciphertext,按字符串原样读出
            parts.append(pd.read_excel(entry["num_enc"], dtype=str).reset_index(drop=True))
        if not parts:
            continue
        merged = pd.concat(parts, axis=1)
        order = [c for c in entry.get("col_order", []) if c in merged.columns]
        if order:
            merged = merged[order]
        # 不带 chart 键:密文不可作图
        sheets.append({"sheet_name": str(entry.get("sheet_name") or "结果")[:31], "df": merged})
    if not sheets:
        return None
    ts = (run_at or "").replace(":", "").replace("-", "").replace("T", "_")[:15] or secrets.token_hex(3)
    dst = cipher_dir / f"{_sanitize(stem, 'result')}_密文_{ts}.xlsx"
    seq = 2
    while dst.exists():
        dst = cipher_dir / f"{_sanitize(stem, 'result')}_密文_{ts}_{seq}.xlsx"
        seq += 1
    write_skill_results(sheets, path=dst)
    return dst


def decrypt_persisted_run_to_excel(run_id: str, stem: str = "analysis"):
    """
    把一次加密暂存的结果(run_id 沙盒)解密为单个明文 Excel,返回落盘路径(~/Downloads)。
    用于交互式「保留密文」后,用户点「解密」按钮事后解出明文。复用产品级渲染器。
    """
    from client.webui.writer import write_skill_results, make_excel_path

    run_dir = ENC_RESULTS_DIR / run_id
    mf = run_dir / "manifest.json"
    if not mf.exists():
        raise FileNotFoundError(f"加密暂存已不存在(run_id={run_id}),无法解密")
    manifest = json.loads(mf.read_text(encoding="utf-8"))
    sheets: list[dict] = []
    for entry in manifest:
        df = _decrypt_one_sheet(entry)
        if df is None or getattr(df, "empty", True):
            continue
        item = {"sheet_name": str(entry.get("sheet_name") or "结果")[:31], "df": df}
        for k in ("chart", "charts", "tier_col", "total_row", "note", "number_formats"):
            v = entry.get(k)
            if v:
                item[k] = v
        sheets.append(item)
    if not sheets:
        raise ValueError("暂存结果为空,无法解密")
    # 交互式事后解密 → 写暂存目录(不自动落 Downloads,用户点「下载」才存)
    return write_skill_results(sheets, path=make_excel_path(stem, staging=True))


def _decrypt_one_sheet(entry: dict):
    """按 manifest 单条还原一张明文 df。"""
    import pandas as pd

    num_plain = None
    if entry.get("num_enc") and Path(entry["num_enc"]).exists():
        from client.tools.runtime import Runtime
        Runtime.get().ensure_all_initialized()
        import crypto_toolkit as ct  # noqa: F401
        import pandaseal as ps  # noqa: F401
        try:
            cdf = ps.read_excel(entry["num_enc"], index_col=0)
        except Exception:
            cdf = ps.read_excel(entry["num_enc"])
        num_plain = ct.decrypt_df(cdf).reset_index(drop=True)
        # 列名对齐(encrypt 往返后列名应保持;兜底用 numeric_cols)
        if list(num_plain.columns) != entry.get("numeric_cols", []):
            try:
                num_plain.columns = entry["numeric_cols"][: len(num_plain.columns)]
            except Exception:
                pass

    id_plain = None
    if entry.get("id_csv") and Path(entry["id_csv"]).exists():
        id_plain = pd.read_csv(entry["id_csv"]).reset_index(drop=True)

    if id_plain is not None and num_plain is not None:
        merged = pd.concat([id_plain, num_plain], axis=1)
    elif num_plain is not None:
        merged = num_plain
    elif id_plain is not None:
        merged = id_plain
    else:
        merged = pd.DataFrame()

    # 还原原始列顺序
    order = [c for c in entry.get("col_order", []) if c in merged.columns]
    if order:
        merged = merged[order]
    return merged


def decrypt_runs_to_folder(runs: list[dict], folder_name: str,
                           output_folder: str = "") -> tuple[Path, list[dict]]:
    """
    批量解密多次运行 → 明文 Excel 落盘。

    - 指定了 output_folder(每任务专属):明文落 <output_folder>/明文/(密文版已在 密文/)。
    - 否则:回退 ~/Downloads/<folder_name>/。

    runs: [{run_id, run_at, manifest: [...], question}] —— 每次运行 → 一个 Excel(多 sheet)。

    返回 (文件夹路径, 逐 run 结果):[{run_id, ok, file, sheets, error}]
    —— 调用方**只对 ok=True 的 run** 做 mark_decrypted / 清沙盒;
       ok=False 的 run 保持待批、密文保留,可重试排查。
    """
    from datetime import datetime

    from client.webui.writer import write_skill_results

    safe = _sanitize(folder_name)
    if output_folder:
        _, out_dir = task_output_subdirs(output_folder)   # <folder>/明文/
    else:
        downloads = Path.home() / "Downloads"
        out_dir = downloads / safe
        if out_dir.exists():
            out_dir = downloads / f"{safe}_{datetime.now().strftime('%H%M%S')}"
    out_dir.mkdir(parents=True, exist_ok=True)

    outcomes: list[dict] = []
    used_names: set[str] = set()
    for run in runs:
        rid = str(run.get("run_id") or "")
        manifest = run.get("manifest", []) or []
        ts = (run.get("run_at") or "").replace(":", "").replace("-", "").replace("T", "_")[:15]
        base = f"{safe}_{ts or secrets.token_hex(3)}"
        # 同一秒完成的多次运行会得到相同时间戳 —— 文件名去重,杜绝互相覆盖
        fname = f"{base}.xlsx"
        seq = 2
        while fname in used_names or (out_dir / fname).exists():
            fname = f"{base}_{seq}.xlsx"
            seq += 1
        dst = out_dir / fname

        try:
            # 还原每张表 + 暂存的呈现键(chart/档位/合计行…),交给产品级渲染器 ——
            # 和单独提问出的 Excel 完全同一套样式(图表/档位上色/合计/自动筛选)
            sheets: list[dict] = []
            for entry in manifest:
                df = _decrypt_one_sheet(entry)
                if df is None or df.empty:
                    continue
                item = {"sheet_name": str(entry.get("sheet_name") or "结果")[:31], "df": df}
                for k in ("chart", "charts", "tier_col", "total_row", "note", "number_formats"):
                    v = entry.get(k)
                    if v:
                        item[k] = v
                sheets.append(item)
            if sheets:
                write_skill_results(sheets, path=dst)
                used_names.add(fname)
                outcomes.append({"run_id": rid, "ok": True, "file": fname,
                                 "sheets": len(sheets), "error": ""})
            else:
                outcomes.append({
                    "run_id": rid, "ok": False, "file": "", "sheets": 0,
                    "error": "密文暂存缺失或解密为空(沙盒文件不存在)· 该次运行保留待批",
                })
        except Exception as e:  # noqa: BLE001 —— 单个 run 失败不拖垮整批
            outcomes.append({"run_id": rid, "ok": False, "file": "", "sheets": 0,
                             "error": f"{type(e).__name__}: {e}"})

    # 一个文件都没写出来 → 收掉空文件夹
    if not any(o["ok"] for o in outcomes):
        try:
            out_dir.rmdir()
        except Exception:
            pass

    return out_dir, outcomes


def cleanup_runs(run_ids: list[str]) -> None:
    """解密完成后删掉沙盒里的密文暂存。"""
    import shutil
    for rid in run_ids:
        d = ENC_RESULTS_DIR / rid
        try:
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass
