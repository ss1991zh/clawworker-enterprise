"""
计算层 · henumpy(hp)—— 数值与矩阵密文运算(场景 2)。

backend="stub":使用既有的明文-on-假密文实现(测试用)
backend="real":使用真实 henumpy 包,API 对照见 zionskill/henumpy-skill/INDEX.md
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from client.tools.crypto import CryptoToolkit, _stub_decrypt, _stub_encrypt
from client.tools.runtime import Runtime
from shared.contract import Operation

SUPPORTED_OPS = [
    "sum",  # params: {"axis": None|int}
    "mean",
    "std",
    "var",
    "dot",  # data={"a":[...], "b":[...]} (stub) 或 CipherArray pair (real)
    "matmul",  # data={"A":..., "B":...}
    "corrcoef",
    "cov",
    "mul",  # 二元运算
    "add",
    "sub",
    "div",
    "transpose",
]


class HENumpy:
    """numpy 风格的密文计算。"""

    name = "henumpy"
    supported_ops = SUPPORTED_OPS

    def __init__(self, backend: str = "stub", evk_path=None):
        self.backend = backend
        self.evk_path = evk_path

    def _is_real(self) -> bool:
        return self.backend == "real"

    def run(self, ops: list[Operation], cipher_in: Any) -> Any:
        if self._is_real():
            return self._run_real(ops, cipher_in)
        return self._run_stub(ops, cipher_in)

    # ----- 真实实现 -----
    def _run_real(self, ops: list[Operation], cipher_in: Any) -> Any:
        Runtime.get().ensure_all_initialized()
        import henumpy as hp

        # cipher_in 期望是 CipherArray;若是 bytes/明文,先加密
        data = self._normalize_input_real(cipher_in)
        for op in ops:
            data = self._apply_op_real(hp, data, op)
        return data

    @staticmethod
    def _normalize_input_real(cipher_in: Any) -> Any:
        """允许传入 CipherArray、ndarray、list。后两者就地加密。"""
        type_name = type(cipher_in).__name__
        if type_name == "CipherArray":
            return cipher_in
        ct = CryptoToolkit(backend="real")
        return ct.encrypt(cipher_in)

    def _apply_op_real(self, hp, data, op: Operation) -> Any:
        name = op.op
        p = op.params or {}
        if name == "sum":
            return hp.sum(data, axis=p.get("axis"))
        if name == "mean":
            return hp.mean(data, axis=p.get("axis"))
        if name == "std":
            return hp.std(data, axis=p.get("axis"), ddof=p.get("ddof", 0))
        if name == "var":
            return hp.var(data, axis=p.get("axis"), ddof=p.get("ddof", 0))
        if name == "transpose":
            return hp.transpose(data)
        if name == "dot":
            # 期望 data 已经是 (a, b) 元组或我们在前面准备好的
            if isinstance(data, dict) and "a" in data and "b" in data:
                a = self._normalize_input_real(data["a"])
                b = self._normalize_input_real(data["b"])
                return hp.dot(a, b)
            raise ValueError("real dot 需要 data={'a':..., 'b':...} 形式")
        if name == "matmul":
            if isinstance(data, dict) and "A" in data and "B" in data:
                A = self._normalize_input_real(data["A"])
                B = self._normalize_input_real(data["B"])
                return hp.matmul(A, B)
            raise ValueError("real matmul 需要 data={'A':..., 'B':...} 形式")
        if name == "corrcoef":
            # hp.corrcoef 需要两个 CipherArray
            if isinstance(data, dict):
                cols = list(data.values())
                # MVP:只取前两列做 corrcoef(未来扩展成全矩阵)
                if len(cols) >= 2:
                    a = self._normalize_input_real(cols[0])
                    b = self._normalize_input_real(cols[1])
                    return hp.corrcoef(a, b)
            raise ValueError("real corrcoef 需要 data 是字段-值字典")
        raise NotImplementedError(f"henumpy real backend 暂未翻译 op: {name}")

    # ===========================================================================
    # Stub 实现(沿用原有)
    # ===========================================================================
    def _run_stub(self, ops: list[Operation], cipher_in: bytes) -> bytes:
        data: Any = _stub_decrypt(cipher_in)
        for op in ops:
            data = self._apply_op_stub(data, op)
        return _stub_encrypt(data)

    def _apply_op_stub(self, data: Any, op: Operation) -> Any:
        name = op.op
        if name == "sum":
            return _flat_sum(data)
        if name == "mean":
            return _flat_mean(data)
        if name == "dot":
            if isinstance(data, dict) and "a" in data and "b" in data:
                a, b = data["a"], data["b"]
                return sum(x * y for x, y in zip(a, b))
            raise ValueError("dot 需要 data={'a':[...], 'b':[...]}")
        if name == "transpose":
            if isinstance(data, list) and data and isinstance(data[0], list):
                return [list(row) for row in zip(*data)]
            raise ValueError("transpose 需要二维 list")
        if name == "matmul":
            if isinstance(data, dict) and "A" in data and "B" in data:
                A, B = data["A"], data["B"]
                return _matmul(A, B)
            raise ValueError("matmul 需要 data={'A':[[...]], 'B':[[...]]}")
        if name == "corrcoef":
            if isinstance(data, dict):
                cols = list(data.values())
                names = list(data.keys())
                n = len(cols)
                mat = [[0.0] * n for _ in range(n)]
                for i in range(n):
                    for j in range(n):
                        mat[i][j] = _corr(cols[i], cols[j])
                return {"labels": names, "matrix": mat}
            raise ValueError("corrcoef 需要 data 为 {字段:[值...]} 字典")
        raise NotImplementedError(f"henumpy 暂不支持 op: {name}")


# ===========================================================================
# Stub helpers(保持原样)
# ===========================================================================


def _flat_sum(data: Any) -> float:
    if isinstance(data, (int, float)):
        return float(data)
    if isinstance(data, list):
        return float(sum(_flat_sum(x) for x in data))
    return 0.0


def _flat_mean(data: Any) -> float:
    nums = _flatten(data)
    return sum(nums) / len(nums) if nums else 0.0


def _flatten(data: Any) -> list[float]:
    if isinstance(data, (int, float)):
        return [float(data)]
    if isinstance(data, list):
        out: list[float] = []
        for x in data:
            out.extend(_flatten(x))
        return out
    return []


def _matmul(A: list[list[float]], B: list[list[float]]) -> list[list[float]]:
    rows_A, cols_A = len(A), len(A[0])
    rows_B, cols_B = len(B), len(B[0])
    if cols_A != rows_B:
        raise ValueError("矩阵尺寸不匹配")
    out = [[0.0] * cols_B for _ in range(rows_A)]
    for i in range(rows_A):
        for j in range(cols_B):
            out[i][j] = sum(A[i][k] * B[k][j] for k in range(cols_A))
    return out


def _corr(x: list[float], y: list[float]) -> float:
    if len(x) != len(y) or not x:
        return 0.0
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = (sum((a - mx) ** 2 for a in x)) ** 0.5
    dy = (sum((b - my) ** 2 for b in y)) ** 0.5
    return num / (dx * dy) if dx and dy else 0.0
