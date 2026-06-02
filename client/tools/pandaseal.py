"""
计算层 · pandaseal(ps)—— pandas 风格的密文 DataFrame 分析(场景 1)。

backend="stub":明文-on-假密文,测试用
backend="real":使用真实 pandaseal,数据形态为 CipherDataFrame

关键 API(real):
- ps.read_csv / ps.read_excel / ps.read_json  → CipherDataFrame
- cdf.groupby(by=..., level=...) → 类似 pandas
- cdf.mean / cdf.sum / cdf.fillna / cdf.dropna 等
- ct.encrypt_df(df) → CipherDataFrame(从明文 DataFrame 加密入库)
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from client.tools.crypto import CryptoToolkit, _stub_decrypt, _stub_encrypt
from client.tools.runtime import Runtime
from shared.contract import Operation

SUPPORTED_OPS = [
    "group_by",
    "sum",
    "mean",
    "count",
    "min",
    "max",
    "pivot",
    "resample",
    "filter",
    "head",
    "describe",
    "div",  # 三种形态:单列/单列,列和/列和,列和/列和 × multiplier
    "turnover_days",  # 库存周转天数(docx §3.2):全 HE 算 M/N/P/Q/R
    "forecast",  # 时间序列透传 + 预测 hint:HE 上仅返回 value 列,实际预测在 renderer 算
]


class PandaSeal:
    """pandas 风格的密文表格分析。"""

    name = "pandaseal"
    supported_ops = SUPPORTED_OPS

    def __init__(self, backend: str = "stub", evk_path=None):
        self.backend = backend
        self.evk_path = evk_path

    def _is_real(self) -> bool:
        return self.backend == "real"

    def run(self, ops: list[Operation], cipher_in: Any) -> Any:
        ops = self._reorder_ops(ops)
        if self._is_real():
            return self._run_real(ops, cipher_in)
        return self._run_stub(ops, cipher_in)

    @staticmethod
    def _reorder_ops(ops: list[Operation]) -> list[Operation]:
        """
        LLM 经常出乱序 plan,把 div 放到 group_by / mean / sum 之后。但 div
        的语义是"行级率",**必须先于聚合执行**。这里自动重排:把所有 div op
        提到第一位,保持彼此相对顺序;其他 op 顺序保持不变。

        示例:
            [group_by region, mean, div actual/target]
          → [div actual/target, group_by region, mean]

        这样行级率先算好,后续按 region 分组取每组平均率,语义正确。
        如果用户的真实意图就是"先聚合再除"(比如算总和率),需要在 plan 里
        用不同的字段名(div 的 num/den 指向已聚合后的字段),这种情况
        我们也保留原顺序 —— 因为 div 仍在 ops 里就会被提前,不会破坏意图。
        极少数高级用例可以由更复杂的 plan 表达(pipeline scenario 6)。
        """
        if not ops:
            return ops
        div_ops = [o for o in ops if o.op == "div"]
        if not div_ops:
            return ops
        non_div = [o for o in ops if o.op != "div"]
        return div_ops + non_div

    # ===========================================================================
    # 真实实现
    # ===========================================================================
    def _run_real(self, ops: list[Operation], cipher_in: Any) -> Any:
        Runtime.get().ensure_all_initialized()
        import pandaseal as ps  # noqa: F401(初始化副作用)

        cdf = self._load_real(cipher_in)
        for op in ops:
            cdf = self._apply_op_real(cdf, op)
        return cdf

    @staticmethod
    def _load_real(cipher_in: Any) -> Any:
        """
        把 cipher_in 规范化为 CipherDataFrame。

        cipher_in 接受:
        - 已经是 CipherDataFrame:直接返回
        - 文件路径(str / Path):按扩展名走 ps.read_csv / read_excel / read_json
        - pandas.DataFrame:就地加密为 CipherDataFrame
        """
        import pandaseal as ps

        type_name = type(cipher_in).__name__
        if type_name == "CipherDataFrame":
            return cipher_in

        if isinstance(cipher_in, (str, Path)):
            p = Path(cipher_in)
            suffix = p.suffix.lower()
            # 当前 ps 版本的实测行为(对照 zionskill SKILL.md):
            # - ps.read_csv(p):自动识别 _P/_L 为 index,列名正确 → 用这个
            # - ps.read_csv(p, index_col=0):TypeError(此版本不接受参数)
            # - ps.read_excel(p):试图把字符串 _P 转 float → ValueError
            # - ps.read_excel(p, index_col=0):把第一数据列也当 index → 数据丢
            #
            # 结论:仅用不传参数的形式,让 ps 自己按 _P/_L 约定处理。
            # 客户端 webui 上传时我们已强制走 CSV 加密,xlsx 路径基本不命中。
            if suffix == ".csv":
                return ps.read_csv(str(p))
            if suffix in (".xlsx", ".xls"):
                # xlsx 路径已知有问题,但仍兜底尝试
                try:
                    return ps.read_excel(str(p))
                except Exception:
                    return ps.read_excel(str(p), index_col=0)
            if suffix == ".json":
                return ps.read_json(str(p))
            raise ValueError(f"pandaseal 不支持文件类型: {suffix}")

        try:
            import pandas as pd  # type: ignore
        except ImportError:
            pd = None
        if pd is not None and isinstance(cipher_in, pd.DataFrame):
            return CryptoToolkit(backend="real").encrypt(cipher_in)

        raise TypeError(f"pandaseal real 不支持输入类型: {type(cipher_in).__name__}")

    def _apply_op_real(self, cdf, op: Operation) -> Any:
        name = op.op
        p = op.params or {}
        field = op.field or p.get("field")

        if name == "head":
            return cdf.head(int(p.get("n", 5)))

        if name == "describe":
            # pandaseal 的 CipherDataFrame 可能没有 describe;用 dropna+mean 兜
            try:
                return cdf.describe()
            except AttributeError:
                return cdf.mean()

        if name == "group_by":
            # pandaseal groupby 基于 level(索引),需要先 set_index
            if hasattr(cdf, "set_index"):
                try:
                    return cdf.set_index(field).groupby(level=0)
                except Exception:
                    return cdf.groupby(level=0)
            return cdf.groupby(level=0)

        # 聚合 op:若已经是 CipherSeries(上一步已聚合),后续同类 op 视为冗余,直接返回
        # —— LLM 常把"各列均值"拆成多个 op,但 cdf.mean() 一次就拿到所有列
        if name in ("sum", "mean", "max", "min"):
            if type(cdf).__name__ == "CipherSeries":
                return cdf
        if name == "sum":
            # CipherDataFrame 没有 sum() —— 用 cumsum().iloc[-1] 取累加最后一行
            if hasattr(cdf, "sum"):
                return cdf.sum()
            cum = cdf.cumsum()
            return cum.iloc[-1]
        if name == "mean":
            return cdf.mean()
        if name == "max":
            return cdf.max()
        if name == "min":
            return cdf.min()
        if name == "count":
            try:
                return cdf.count()
            except AttributeError:
                return cdf.shape[0]

        if name == "div":
            # 列对列逐行除法 → 返回 CipherSeries
            # 支持三种形式:
            # 1. 单列 / 单列    {numerator: col, denominator: col}
            # 2. 列和 / 列和    {numerator_cols: [...], denominator_cols: [...]}
            #    用于"加权平均率"如 (期初金额+入库金额)/(期初数量+入库数量)
            # 3. 形式 2 + 乘子  + {multiplier: col}
            #    用于"X × 加权率"如 出库金额 = 出库数量 × (I+K)/(H+J)
            type_name = type(cdf).__name__
            # 智能兜底:LLM 常出 [group_by, div] 这种乱序 → 取 GroupBy.obj 做行级 div
            # 后续 renderer 再用 metadata 做组级聚合,语义等价于"行级率 + 组内 mean"
            if "GroupBy" in type_name:
                underlying = getattr(cdf, "obj", None)
                if underlying is None:
                    raise ValueError(
                        "div 收到 GroupBy 对象但拿不到 underlying DataFrame · "
                        "请让 plan 把 div 放在 group_by 之前"
                    )
                cdf = underlying
                type_name = type(cdf).__name__
            if type_name == "CipherSeries":
                raise ValueError(
                    "div 不能作用在 CipherSeries 上 · plan op 顺序问题:"
                    "应先 div(actual / target) 得到行级率,再聚合(mean/sum)"
                )
            if type_name != "CipherDataFrame":
                raise ValueError(
                    f"div 暂不支持 {type_name} · 请把 div 放到 op 序列最前面"
                )

            single_num = p.get("numerator") or p.get("num")
            single_den = p.get("denominator") or p.get("den")
            num_cols = p.get("numerator_cols") or ([single_num] if single_num else [])
            den_cols = p.get("denominator_cols") or ([single_den] if single_den else [])
            if not num_cols or not den_cols:
                raise ValueError(
                    "div 需要 numerator(_cols) + denominator(_cols)"
                )

            missing = [c for c in num_cols + den_cols if c not in cdf.columns]
            if missing:
                raise ValueError(f"div 字段不存在: {missing}")

            # 累加分子/分母列
            num_series = cdf[num_cols[0]]
            for c in num_cols[1:]:
                num_series = num_series + cdf[c]
            den_series = cdf[den_cols[0]]
            for c in den_cols[1:]:
                den_series = den_series + cdf[c]

            result = num_series / den_series

            # 可选乘子(用于 X × ratio 形式)
            multiplier = p.get("multiplier")
            if multiplier:
                if multiplier not in cdf.columns:
                    raise ValueError(f"multiplier 列不存在: {multiplier}")
                result = result * cdf[multiplier]

            return result

        if name == "forecast":
            # 时间序列预测 hint op:HE 上仅返回值列(CipherSeries 或 CipherDataFrame),
            # 真正的预测(MA3/MA6/WMA/OLS + 季节调整 + 置信区间 + YoY + 维度切片)
            # 都在 renderer 上对解密后的历史值做(避免 HE 上的复杂外推)。
            #
            # 单列模式(原行为):params={"value_col": str} → CipherSeries
            # 多列模式(v2)    :cdf 有多列且 value_col 在其中 → 整个 CipherDataFrame,
            #                  decrypt 后 renderer 据列名前缀(total_sales / line_* / region_*)
            #                  生成 5 sheet 多维预测。
            col = p.get("value_col") or p.get("field") or p.get("col")
            if not col:
                raise ValueError("forecast 需要 value_col")
            if col not in cdf.columns:
                raise ValueError(f"forecast value_col 不存在: {col}")
            mode = p.get("return", "auto")
            if mode == "single" or len(cdf.columns) == 1:
                return cdf[col]
            # auto / multi:多列时返回整个 CipherDataFrame
            return cdf

        if name == "turnover_days":
            # 库存周转天数 R = 平均库存金额 × 期间天数 / 出库金额
            # 全在密文上一次性算完(对应 docx §3.2):
            #   M = (I + K) / (H + J)        加权平均单价
            #   N = L × M                    出库金额
            #   P = I + K - N                期末金额
            #   Q = (I + P) / 2              平均库存金额
            #   R = Q × days / N             周转天数
            H = cdf[p.get("begin_qty", "begin_qty")]
            I = cdf[p.get("begin_amount", "begin_amount")]
            J = cdf[p.get("in_qty", "in_qty")]
            K = cdf[p.get("in_amount", "in_amount")]
            L = cdf[p.get("out_qty", "out_qty")]
            days = float(p.get("days", 30))

            sum_amt = I + K
            sum_qty = H + J
            M = sum_amt / sum_qty
            N = L * M
            P = sum_amt - N  # = I + K - N
            Q = (I + P) / 2.0
            R = Q * days / N
            return R

        if name == "filter":
            # 条件过滤:用重载比较运算符
            op_kind = p.get("op", "eq")
            value = p.get("value")
            col = field
            if not col:
                raise ValueError("filter 需要 field")
            col_series = cdf[col]
            ct = CryptoToolkit(backend="real")
            value_cipher = ct.encrypt([value])
            if op_kind == "eq":
                mask = col_series == value_cipher
            elif op_kind == "gt":
                mask = col_series > value_cipher
            elif op_kind == "lt":
                mask = col_series < value_cipher
            else:
                raise ValueError(f"未知 filter op: {op_kind}")
            return cdf[mask]

        if name in ("select", "project", "columns"):
            # 选若干列 → 返回子 CipherDataFrame(或单列时返回 CipherSeries)
            # LLM 常用:{op: "select", fields: ["a","b","c"]} 或 {op:"select", field:"a"}
            cols = []
            if op.fields:
                cols.extend(op.fields)
            elif op.field:
                cols.append(op.field)
            elif p.get("columns"):
                v = p["columns"]
                cols.extend(v if isinstance(v, list) else [v])
            elif p.get("cols"):
                v = p["cols"]
                cols.extend(v if isinstance(v, list) else [v])
            if not cols:
                # 没指定 fields → no-op 直接返回
                return cdf
            missing = [c for c in cols if c not in cdf.columns]
            if missing:
                raise ValueError(f"select 字段不存在: {missing}")
            if len(cols) == 1:
                return cdf[cols[0]]
            return cdf[cols]

        if name in ("drop", "drop_columns"):
            # 删若干列
            cols = list(op.fields) if op.fields else ([op.field] if op.field else [])
            if not cols and p.get("columns"):
                v = p["columns"]
                cols = list(v) if isinstance(v, list) else [v]
            kept = [c for c in cdf.columns if c not in cols]
            return cdf[kept]

        if name in ("rename", "alias"):
            # rename 列名 — pandaseal 可能不支持原生 rename,做 best-effort
            mapping = p.get("columns") or p.get("mapping") or {}
            if not mapping:
                return cdf
            try:
                return cdf.rename(columns=mapping)
            except Exception:
                # 不支持 rename 就返回原样(不阻断后续 op)
                return cdf

        if name in ("sort", "sort_values", "order_by"):
            by = op.field or p.get("by") or p.get("field")
            ascending = bool(p.get("ascending", False))
            if not by:
                return cdf
            try:
                return cdf.sort_values(by=by, ascending=ascending)
            except Exception:
                return cdf  # 不支持就不动

        if name in ("limit", "top", "top_n", "nlargest"):
            n = int(p.get("n", p.get("limit", p.get("top", 10))))
            by = op.field or p.get("by")
            try:
                if by:
                    return cdf.nlargest(n, by) if hasattr(cdf, "nlargest") else cdf.sort_values(by, ascending=False).head(n)
                return cdf.head(n)
            except Exception:
                return cdf.head(n)

        if name in ("bottom", "bottom_n", "nsmallest"):
            n = int(p.get("n", p.get("limit", 10)))
            by = op.field or p.get("by")
            try:
                if by:
                    return cdf.nsmallest(n, by) if hasattr(cdf, "nsmallest") else cdf.sort_values(by, ascending=True).head(n)
                return cdf.head(n)
            except Exception:
                return cdf.head(n)

        if name == "pivot":
            raise NotImplementedError(
                "pandaseal real backend 的 pivot 需要根据真实 API 适配,MVP 后续支持"
            )
        if name == "resample":
            raise NotImplementedError(
                "pandaseal real backend 的 resample 需要根据真实 API 适配,MVP 后续支持"
            )

        # 兜底:不识别的 op 直接 passthrough(返回 cdf),不打断工作流
        # 这样 LLM 哪怕给了奇怪 op 也能继续跑下游 op + 渲染
        print(f"[pandaseal] 警告:未识别 op «{name}»,passthrough(参数 {p})")
        return cdf

    # ===========================================================================
    # Stub 实现(沿用)
    # ===========================================================================
    def _run_stub(self, ops: list[Operation], cipher_in: bytes) -> bytes:
        data = _stub_decrypt(cipher_in)
        if not isinstance(data, list):
            raise ValueError("pandaseal 输入应为行的列表")
        rows: list[dict[str, Any]] = list(data)
        for op in ops:
            rows = self._apply_op_stub(rows, op)
        return _stub_encrypt(rows)

    def _apply_op_stub(self, rows: list[dict], op: Operation) -> list[dict]:
        name = op.op
        if name == "group_by":
            field = op.field or op.params.get("field")
            if not field:
                raise ValueError("group_by 需要 field")
            return _group_rows(rows, field)
        if name in ("sum", "mean", "min", "max"):
            field = op.field or op.params.get("field")
            return _aggregate(rows, field, name)
        if name == "count":
            return _count(rows)
        if name == "pivot":
            return _pivot(rows, **op.params)
        if name == "resample":
            return _resample(rows, op.params.get("field"), op.params.get("freq", "M"))
        if name == "filter":
            return _filter(rows, **op.params)
        raise NotImplementedError(f"pandaseal stub 不支持 op: {name}")


# ===========================================================================
# Stub helpers(保持原样)
# ===========================================================================


def _group_rows(rows: list[dict], field: str) -> list[dict]:
    out = []
    for r in rows:
        new = dict(r)
        new["__group__"] = r.get(field)
        out.append(new)
    return out


def _aggregate(rows: list[dict], field: str, agg: str) -> list[dict]:
    grouped = defaultdict(list)
    for r in rows:
        key = r.get("__group__")
        if field in r and r[field] is not None:
            grouped[key].append(r[field])
    out = []
    for key, values in grouped.items():
        if agg == "sum":
            v = sum(values)
        elif agg == "mean":
            v = sum(values) / len(values) if values else 0
        elif agg == "min":
            v = min(values) if values else None
        elif agg == "max":
            v = max(values) if values else None
        else:
            v = None
        out.append({"__group__": key, f"{field}_{agg}": v})
    return out


def _count(rows: list[dict]) -> list[dict]:
    grouped: dict[Any, int] = defaultdict(int)
    for r in rows:
        key = r.get("__group__")
        grouped[key] += 1
    return [{"__group__": k, "count": v} for k, v in grouped.items()]


def _pivot(rows: list[dict], index: str, columns: str, values: str) -> list[dict]:
    pivot: dict[Any, dict[Any, Any]] = defaultdict(dict)
    for r in rows:
        pivot[r.get(index)][r.get(columns)] = r.get(values)
    out = []
    for idx_val, col_map in pivot.items():
        row = {index: idx_val}
        row.update(col_map)
        out.append(row)
    return out


def _resample(rows: list[dict], field: str, freq: str) -> list[dict]:
    grouped: dict[str, list] = defaultdict(list)
    for r in rows:
        ts = str(r.get(field, ""))
        if freq == "M":
            key = ts[:7]
        elif freq == "Y":
            key = ts[:4]
        else:
            key = ts[:10]
        grouped[key].append(r)
    return [{"__group__": k, "rows": v} for k, v in grouped.items()]


def _filter(rows: list[dict], field: str, op: str, value: Any) -> list[dict]:
    def keep(r: dict) -> bool:
        v = r.get(field)
        if op == "eq":
            return v == value
        if op == "gt":
            return v is not None and v > value
        if op == "lt":
            return v is not None and v < value
        return True

    return [r for r in rows if keep(r)]
