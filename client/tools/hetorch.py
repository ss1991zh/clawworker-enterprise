"""
计算层 · hetorch —— 深度学习推理(场景 4)。

⚠️ STUB:用极简明文推理模拟。
   注意:HE 下只做推理,不做训练;真实 hetorch SDK 通常会:
     - 加载预训练模型(可能也是密文形式)
     - 对加密输入做前向推理
     - 返回加密结果(标签 + 置信度)
"""

from __future__ import annotations

from typing import Any

from client.tools.crypto import _stub_decrypt, _stub_encrypt
from shared.contract import Operation

SUPPORTED_OPS = [
    "forward",  # params: {"model_id": str}
    "embed",  # params: {"model_id": str, "dim": int}
    "classify",  # params: {"model_id": str, "n_classes": int}
]


class HETorch:
    """pytorch 类:在密文输入上做神经网络前向推理。

    ⚠️ 密态数据分析包里暂未提供 hetorch2,等用户后续提供。
       backend="real" 当前会抛 NotImplementedError 提示用户提供 SDK。
    """

    name = "hetorch"
    supported_ops = SUPPORTED_OPS

    def __init__(self, backend: str = "stub", evk_path=None, model_dir=None):
        self.backend = backend
        self.evk_path = evk_path
        self.model_dir = model_dir

    def _is_real(self) -> bool:
        return self.backend == "real"

    def run(self, ops: list[Operation], cipher_in: bytes) -> bytes:
        if self._is_real():
            raise NotImplementedError(
                "hetorch2 包尚未在密态数据分析目录中提供,real backend 暂不可用。"
                "请在 PROVIDE_ME.md 中跟进 hetorch2 SDK。"
            )
        data: Any = _stub_decrypt(cipher_in)
        for op in ops:
            data = self._apply_op(data, op)
        return _stub_encrypt(data)

    def _apply_op(self, data: Any, op: Operation) -> Any:
        name = op.op
        if name == "classify":
            return _stub_classify(data, op.params)
        if name == "embed":
            return _stub_embed(data, op.params)
        if name == "forward":
            return _stub_forward(data, op.params)
        raise NotImplementedError(f"hetorch 暂不支持 op: {name}")


def _stub_classify(X: list[list[float]], params: dict) -> list[dict]:
    """对每个输入返回一个 (label, confidence) 字典。"""
    n_classes = int(params.get("n_classes", 2))
    out = []
    for row in X:
        # 极简:用 sum mod n 作 label,softmax 的 confidence 用伪造均匀分布
        label = int(abs(sum(row))) % n_classes
        out.append({"label": label, "confidence": 1.0 / n_classes})
    return out


def _stub_embed(X: list[list[float]], params: dict) -> list[list[float]]:
    """输出与输入等量的伪 embedding 向量。"""
    dim = int(params.get("dim", 4))
    out = []
    for row in X:
        emb = [(sum(row) / max(1, len(row))) * (i + 1) for i in range(dim)]
        out.append(emb)
    return out


def _stub_forward(X: list[list[float]], params: dict) -> list[float]:
    """通用前向:逐行返回均值。"""
    return [sum(row) / max(1, len(row)) for row in X]
