"""
计算层 · helearn(hl)—— sklearn 风格密文 ML(场景 3)。

真实 API:
- hl.LinearRegression / hl.LogisticRegression / hl.GradientBoostingClassifier/Regressor
- 接口与 sklearn 对齐:.fit(X, y) / .predict(X) / .score(X, y)
- HE 下推理为主,训练通常在明文侧离线完成
"""

from __future__ import annotations

from typing import Any

from client.tools.crypto import CryptoToolkit, _stub_decrypt, _stub_encrypt
from client.tools.runtime import Runtime
from shared.contract import Operation

SUPPORTED_OPS = [
    "linear_regression_predict",  # params: {"weights":[...], "bias": float}
    "logistic_regression_predict",
    "pca_transform",
    "kmeans_predict",
    "fit_predict",  # 真实 backend:训练+推理(传入 X, y, model_type)
]


class HELearn:
    """sklearn 风格密文机器学习。"""

    name = "helearn"
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

    # ===========================================================================
    # 真实实现
    # ===========================================================================
    def _run_real(self, ops: list[Operation], cipher_in: Any) -> Any:
        Runtime.get().ensure_all_initialized()
        import helearn as hl

        data = self._normalize_input_real(cipher_in)
        for op in ops:
            data = self._apply_op_real(hl, data, op)
        return data

    @staticmethod
    def _normalize_input_real(cipher_in: Any) -> Any:
        """期望 CipherArray 或 dict{X,y};否则加密。"""
        type_name = type(cipher_in).__name__
        if type_name in ("CipherArray", "dict"):
            return cipher_in
        ct = CryptoToolkit(backend="real")
        return ct.encrypt(cipher_in)

    def _apply_op_real(self, hl, data, op: Operation) -> Any:
        name = op.op
        p = op.params or {}

        if name == "fit_predict":
            # data: {"X_train": ..., "y_train": ..., "X_test": ...}
            model_type = p.get("model", "LinearRegression")
            if not isinstance(data, dict):
                raise ValueError("fit_predict 需要 data={'X_train':..., 'y_train':..., 'X_test':...}")
            X_train = self._normalize_input_real(data.get("X_train"))
            y_train = self._normalize_input_real(data.get("y_train"))
            X_test = self._normalize_input_real(data.get("X_test", X_train))
            Model = getattr(hl, model_type, None)
            if Model is None:
                raise ValueError(f"helearn 中无模型: {model_type}")
            model = Model()
            model.fit(X_train, y_train)
            return model.predict(X_test)

        if name == "linear_regression_predict":
            X = data
            model = hl.LinearRegression()
            # 真实 backend 没有"传系数就推理"的直接接口,这里需要先 fit 再 predict
            # 上层应改用 fit_predict;否则本 op 仅作占位
            raise NotImplementedError(
                "real backend 请使用 fit_predict 传入 X_train/y_train/X_test"
            )

        if name == "logistic_regression_predict":
            raise NotImplementedError("real backend 请使用 fit_predict + model='LogisticRegression'")

        if name == "kmeans_predict":
            raise NotImplementedError("helearn 暂未提供 KMeans —— 待真实 SDK 补充")

        if name == "pca_transform":
            raise NotImplementedError("helearn 暂未提供 PCA —— 待真实 SDK 补充")

        raise NotImplementedError(f"helearn real backend 暂不支持 op: {name}")

    # ===========================================================================
    # Stub 实现(沿用)
    # ===========================================================================
    def _run_stub(self, ops: list[Operation], cipher_in: bytes) -> bytes:
        data: Any = _stub_decrypt(cipher_in)
        for op in ops:
            data = self._apply_op_stub(data, op)
        return _stub_encrypt(data)

    def _apply_op_stub(self, data: Any, op: Operation) -> Any:
        name = op.op
        if name == "linear_regression_predict":
            return _linreg_predict(data, op.params)
        if name == "logistic_regression_predict":
            return _logreg_predict(data, op.params)
        if name == "kmeans_predict":
            return _kmeans_predict(data, op.params)
        if name == "pca_transform":
            return _pca_transform(data, op.params)
        raise NotImplementedError(f"helearn stub 不支持 op: {name}")


# ===========================================================================
# Stub helpers
# ===========================================================================


def _linreg_predict(X: list[list[float]], params: dict) -> list[float]:
    weights = params.get("weights", [])
    bias = params.get("bias", 0.0)
    return [sum(w * x for w, x in zip(weights, row)) + bias for row in X]


def _logreg_predict(X: list[list[float]], params: dict) -> list[float]:
    import math

    weights = params.get("weights", [])
    bias = params.get("bias", 0.0)

    def sigmoid(z: float) -> float:
        return 1.0 / (1.0 + math.exp(-z))

    return [sigmoid(sum(w * x for w, x in zip(weights, row)) + bias) for row in X]


def _kmeans_predict(X: list[list[float]], params: dict) -> list[int]:
    centroids: list[list[float]] = params.get("centroids", [])
    labels: list[int] = []
    for row in X:
        best, best_d = -1, float("inf")
        for i, c in enumerate(centroids):
            d = sum((a - b) ** 2 for a, b in zip(row, c))
            if d < best_d:
                best_d, best = d, i
        labels.append(best)
    return labels


def _pca_transform(X: list[list[float]], params: dict) -> list[list[float]]:
    components: list[list[float]] = params.get("components", [])
    mean: list[float] = params.get("mean", [])
    out: list[list[float]] = []
    for row in X:
        centered = [r - m for r, m in zip(row, mean)]
        projected = [sum(c * v for c, v in zip(comp, centered)) for comp in components]
        out.append(projected)
    return out
