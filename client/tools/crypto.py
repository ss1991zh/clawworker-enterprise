"""
加密层 —— crypto_toolkit (ct) 包装。

⚠️ 重要术语订正(架构 v2):
- 此前误以为加解密工具叫 `zfhe`,实际上 `zfhe` 是 zionskill 中的"全流程编排 skill"概念。
- 真正的加解密 Python 包叫 `crypto_toolkit`(别名 `ct`),对应本类 CryptoToolkit。
- 为向后兼容,旧的 `ZFHE` 名字仍作为 CryptoToolkit 的别名保留(测试/CLI 已统一)。

支持两种 backend:
- "stub"(默认):JSON 包装的假密文,所有原有测试照常通过
- "real":使用真实 crypto_toolkit 包,需要密钥文件在位
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from client.tools.runtime import Runtime

STUB_MARKER = "STUB_CIPHER"


# ===========================================================================
# Stub 实现(沿用之前,确保兼容性)
# ===========================================================================


def _is_stub_cipher(blob: bytes) -> bool:
    try:
        obj = json.loads(blob.decode("utf-8"))
        return isinstance(obj, dict) and obj.get("_marker") == STUB_MARKER
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False


def _stub_encrypt(plaintext: Any) -> bytes:
    obj = {"_marker": STUB_MARKER, "_plaintext": plaintext}
    return json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")


def _stub_decrypt(ciphertext: bytes) -> Any:
    if not _is_stub_cipher(ciphertext):
        raise ValueError("非 STUB 密文 —— 请确认 backend 与密文类型匹配")
    obj = json.loads(ciphertext.decode("utf-8"))
    return obj["_plaintext"]


# ===========================================================================
# 真实实现
# ===========================================================================


def _real_encrypt(plaintext: Any) -> Any:
    """
    用 crypto_toolkit 加密。

    输入类型 → API 选择:
      list / ndarray → ct.encrypt(arr)
      pandas.DataFrame → ct.encrypt_df(df)
      torch.Tensor → ct.encrypt_tensor(t)  (需 hetorch2)
    """
    Runtime.get().ensure_all_initialized()
    import crypto_toolkit as ct
    import numpy as np

    try:
        import pandas as pd  # type: ignore
    except ImportError:  # pragma: no cover
        pd = None

    if pd is not None and isinstance(plaintext, pd.DataFrame):
        return ct.encrypt_df(plaintext)
    if isinstance(plaintext, (list, tuple)):
        plaintext = np.array(plaintext, dtype=np.float64)
    if isinstance(plaintext, np.ndarray):
        return ct.encrypt(plaintext)
    raise TypeError(f"crypto_toolkit 暂不支持加密类型: {type(plaintext).__name__}")


def _real_decrypt(ciphertext: Any) -> Any:
    """
    用 crypto_toolkit 解密。

    根据 ciphertext 的实际 Python 类型选择 decrypt / decrypt_df 等。
    CipherSeries(pandaseal 聚合结果)需要按索引展开成 dict。
    """
    Runtime.get().ensure_all_initialized()
    import crypto_toolkit as ct

    type_name = type(ciphertext).__name__
    if type_name == "CipherDataFrame":
        return ct.decrypt_df(ciphertext)
    if type_name == "CipherArray":
        return ct.decrypt(ciphertext)
    if type_name == "CipherTensor":
        return ct.decrypt_tensor(ciphertext)
    if type_name == "CipherSeries":
        # CipherSeries 两种典型形态:
        # (a) 列名索引(列聚合结果,如 cdf.mean()):返回 dict{col_name: val}
        # (b) 整数序列索引(逐行结果,如 cdf['a']/cdf['b']):返回 list[val](与行 1-1 对齐)
        import numpy as np

        def _to_scalar(val):
            try:
                if hasattr(val, "shape") and val.shape == ():
                    return val.item()
                if hasattr(val, "__len__") and len(val) == 1:
                    return val[0]
            except Exception:
                pass
            return val

        idx_values = list(ciphertext.index)
        # 检测是否 row-aligned(整数 0..N-1 序列)
        is_row_aligned = (
            len(idx_values) > 0
            and all(isinstance(k, (int, np.integer)) for k in idx_values)
            and list(idx_values) == list(range(len(idx_values)))
        )
        if is_row_aligned:
            return [_to_scalar(ct.decrypt(ciphertext.iloc[i])) for i in range(len(idx_values))]
        # 列名 / 标签索引
        result: dict = {}
        for key in idx_values:
            val = ct.decrypt(ciphertext.loc[key])
            result[str(key)] = _to_scalar(val)
        return result
    # 兜底:尝试通用解密
    return ct.decrypt(ciphertext)


def _real_encrypt_file(src: Path, dst: Path) -> Path:
    """根据后缀选择 encrypt_csv / encrypt_excel / encrypt_json。

    ⚠️ ct.encrypt_csv 是追加模式 —— 旧文件存在会和新内容叠加导致列数错乱。
       这里在调用前主动删除旧目标文件,确保每次产出干净的新密文。
    """
    Runtime.get().ensure_all_initialized()
    import crypto_toolkit as ct

    # 防御:删除已存在的目标(追加模式陷阱)
    if dst.exists():
        dst.unlink()

    suffix = src.suffix.lower()
    if suffix == ".csv":
        ct.encrypt_csv(str(src), str(dst))
    elif suffix in (".xlsx", ".xls"):
        # ⚠️ ct.encrypt_excel 默认 input_index_col=0,会把"第一列"当 index 不加密。
        # 后续 ps.read_excel(index_col=0) 又把第一列当 index 跳过 → 第一列(如 target)
        # 完全丢失。改成 input_index_col=None,所有列都进密文,后续完整读回。
        ct.encrypt_excel(str(src), str(dst), input_index_col=None)
    elif suffix == ".json":
        ct.encrypt_json(str(src), str(dst))
    else:
        raise ValueError(f"crypto_toolkit 暂不支持加密文件类型: {suffix}")
    return dst


def _real_decrypt_file(src: Path, dst: Path) -> Path:
    Runtime.get().ensure_all_initialized()
    import crypto_toolkit as ct

    suffix = dst.suffix.lower()
    if suffix == ".csv":
        ct.decrypt_csv(str(src), str(dst))
    elif suffix in (".xlsx", ".xls"):
        ct.decrypt_excel(str(src), str(dst))
    elif suffix == ".json":
        ct.decrypt_json(str(src), str(dst))
    else:
        raise ValueError(f"crypto_toolkit 暂不支持解密文件类型: {suffix}")
    return dst


# ===========================================================================
# 统一包装类
# ===========================================================================


class CryptoToolkit:
    """
    加解密原语,backend 决定走 stub 还是真实 crypto_toolkit。

    Args:
        backend: "stub"(默认)或 "real"
        sk_path / evk_path: 密钥路径(real backend 用,可选,缺省走 Runtime 配置)
    """

    name = "crypto_toolkit"

    def __init__(
        self,
        backend: str = "stub",
        sk_path: Optional[Path] = None,
        evk_path: Optional[Path] = None,
    ):
        self.backend = backend
        self.sk_path = sk_path
        self.evk_path = evk_path

    def _is_real(self) -> bool:
        return self.backend == "real"

    # ----- 加密 -----
    def encrypt(self, plaintext: Any) -> Any:
        if self._is_real():
            return _real_encrypt(plaintext)
        return _stub_encrypt(plaintext)

    def encrypt_file(self, src: Path, dst: Path) -> Path:
        if self._is_real():
            return _real_encrypt_file(src, dst)
        raw = src.read_bytes()
        cipher = _stub_encrypt({"_filename": src.name, "_raw_b64": raw.hex()})
        dst.write_bytes(cipher)
        return dst

    # ----- 解密 -----
    def decrypt(self, ciphertext: Any) -> Any:
        if self._is_real():
            return _real_decrypt(ciphertext)
        return _stub_decrypt(ciphertext)

    def decrypt_file(self, src: Path, dst: Path) -> Path:
        if self._is_real():
            return _real_decrypt_file(src, dst)
        obj = _stub_decrypt(src.read_bytes())
        if isinstance(obj, dict) and "_raw_b64" in obj:
            dst.write_bytes(bytes.fromhex(obj["_raw_b64"]))
        else:
            dst.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
        return dst


# 向后兼容别名 —— 旧代码/测试可能引用 ZFHE
ZFHE = CryptoToolkit
