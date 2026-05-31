"""
工具集运行时:backend 选择 + 一次性初始化(ct.initSK / hp.initDict / hetorch2.initDict)。

设计原则:
- 默认 backend="stub",所有原有测试照常通过
- backend="real" 时需要密钥文件在位
- initSK 全局只调一次,initDict 同理
- 检测密钥/字典文件存在性,缺失时给出明确错误,而不是让底层 Go 库 panic
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Optional

Backend = Literal["stub", "real"]

# 真实包的默认密钥/字典路径(由 setup.py 决定)
DEFAULT_SK_PATH = (
    "/Users/davidzheng/Desktop/密态数据分析/crypto_toolkit-64_dev/crypto_toolkit/file/skf"
)
DEFAULT_DICT_DIR = (
    "/Users/davidzheng/Desktop/密态数据分析/henumpy-dev/henumpy/file"
)
DEFAULT_USER_AUTH = (
    "/Users/davidzheng/Desktop/密态数据分析/henumpy-dev/henumpy/file/user_authorization"
)


def get_backend_from_env() -> Backend:
    """通过环境变量 AGENT_BACKEND=stub|real 选择 backend。"""
    val = os.environ.get("AGENT_BACKEND", "stub").lower()
    if val not in ("stub", "real"):
        raise ValueError(f"AGENT_BACKEND 只能是 stub 或 real,得到: {val}")
    return val  # type: ignore[return-value]


def _file_exists(path: str | Path) -> bool:
    try:
        return Path(path).is_file()
    except Exception:
        return False


@dataclass
class RuntimeConfig:
    """运行时配置 —— backend + 密钥/字典路径。"""

    backend: Backend = "stub"
    sk_path: Optional[str] = None
    dict_path: Optional[str] = None
    user_auth_path: Optional[str] = None


class Runtime:
    """
    全局运行时管理(单例 per process)。
    - 记录当前 backend
    - 真实模式下,确保 initSK 与 initDict 只被调一次
    """

    _instance: Optional["Runtime"] = None
    _lock = threading.Lock()

    def __init__(self, config: Optional[RuntimeConfig] = None):
        self.config = config or RuntimeConfig(backend=get_backend_from_env())
        self._sk_initialized = False
        self._dict_initialized = False

    # ----- 单例 -----
    @classmethod
    def get(cls) -> "Runtime":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    @classmethod
    def reset(cls, config: Optional[RuntimeConfig] = None) -> "Runtime":
        """测试用:重置单例。"""
        with cls._lock:
            cls._instance = cls(config) if config else cls()
            return cls._instance

    # ----- 状态查询 -----
    @property
    def backend(self) -> Backend:
        return self.config.backend

    def is_real(self) -> bool:
        return self.config.backend == "real"

    def real_available(self) -> bool:
        """真实 backend 所需的所有密钥/字典文件是否都在位。"""
        sk = self.config.sk_path or DEFAULT_SK_PATH
        if not _file_exists(sk):
            return False
        # henumpy initDict 需要字典文件,默认目录下可能要一组文件 —— MVP 简化为检查目录非空
        dict_dir = Path(self.config.dict_path or DEFAULT_DICT_DIR)
        if not dict_dir.is_dir():
            return False
        # 至少有一个非 file.md 的文件
        has_dict_file = any(p.is_file() and p.name != "file.md" for p in dict_dir.iterdir())
        return has_dict_file

    # ----- 初始化(幂等)-----
    # 注:这两个方法只由真实 backend 的工具调用(stub 工具不会触发)。
    # 因此不再卡 is_real() ——  调用方有责任只在需要真实初始化时调用。
    def ensure_sk_initialized(self) -> None:
        """调用 ct.initSK(sk_path),只调一次。"""
        if self._sk_initialized:
            return
        sk_path = self.config.sk_path or DEFAULT_SK_PATH
        if not _file_exists(sk_path):
            raise RuntimeError(
                f"真实 backend 需要 sk 密钥文件,但未找到: {sk_path}。"
                f"请按 PROVIDE_ME.md 把密钥放到对应位置。"
            )
        import crypto_toolkit as ct

        ct.initSK(skFilePath=sk_path)
        self._sk_initialized = True

    def ensure_dict_initialized(self) -> None:
        """调用 hp.initDict(),只调一次。"""
        if self._dict_initialized:
            return
        dict_path = self.config.dict_path
        user_auth = self.config.user_auth_path or DEFAULT_USER_AUTH
        if not _file_exists(user_auth):
            raise RuntimeError(
                f"真实 backend 需要 user_authorization 文件,但未找到: {user_auth}。"
                f"请按 PROVIDE_ME.md 提供。"
            )
        import henumpy as hp

        if dict_path:
            hp.initDict(dictFilePath=dict_path, userFilePath=user_auth)
        else:
            hp.initDict(userFilePath=user_auth)
        self._dict_initialized = True

    def ensure_all_initialized(self) -> None:
        """计算/加密前的总开关 —— 调一次包揽 initSK + initDict。"""
        self.ensure_sk_initialized()
        self.ensure_dict_initialized()
