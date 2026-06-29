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

# 默认密钥/字典/授权文件解析(跨平台)。优先级:
#   环境变量(AGENT_SK_PATH / AGENT_DICT_DIR / AGENT_USER_AUTH)
#   > 已安装 HE 包内自带文件 <pkg>/file/...(随包走,Windows/Linux/Mac 通用)
#   > 开发机回退路径(仅本机,别处不存在也无碍)。
_FALLBACK_SK = "/Users/davidzheng/Desktop/密态数据分析/crypto_toolkit-64_dev/crypto_toolkit/file/skf"
_FALLBACK_DICT_DIR = "/Users/davidzheng/Desktop/密态数据分析/henumpy-dev/henumpy/file"
_FALLBACK_USER_AUTH = "/Users/davidzheng/Desktop/密态数据分析/henumpy-dev/henumpy/file/user_authorization"


def _pkg_file_dir(module_name: str):
    """已安装 HE 包内自带文件目录 <pkg>/file。失败返回 None。"""
    try:
        import importlib
        m = importlib.import_module(module_name)
        if getattr(m, "__file__", None):
            return Path(m.__file__).resolve().parent / "file"
    except Exception:  # noqa: BLE001
        pass
    return None


# 当前登录用户的 vault 目录(由客户端登录时 set_active_vault 设置)。
# 用户在 UI 里上传的 sk/字典、从主机拉的 user_authorization 都落在这里,
# 引擎优先用 vault → 实现"上传即生效",找不到再回退包目录/环境变量。
_active_vault: Optional[Path] = None


def set_active_vault(path: Optional[str | Path]) -> None:
    """绑定当前用户的密钥 vault 目录(登录后调用)。传 None 解绑。"""
    global _active_vault
    _active_vault = Path(path) if path else None


def _vault_file(name: str) -> Optional[str]:
    if _active_vault is not None:
        p = _active_vault / name
        if p.is_file():
            return str(p)
    return None


def _resolve_sk_path() -> str:
    env = os.environ.get("AGENT_SK_PATH")
    if env:
        return env
    v = _vault_file("sk.bin")          # 用户上传的 sk(vault 里存为 sk.bin)
    if v:
        return v
    d = _pkg_file_dir("crypto_toolkit")
    return str(d / "skf") if d else _FALLBACK_SK


def _resolve_dict_dir() -> str:
    env = os.environ.get("AGENT_DICT_DIR")
    if env:
        return env
    d = _pkg_file_dir("henumpy")
    return str(d) if d else _FALLBACK_DICT_DIR


def _resolve_dict_file() -> Optional[str]:
    """字典文件(henumpy initDict 的 dictFilePath)。
    注:本方案里"计算密钥 evk"与"计算字典 dictf"是同一文件 —— 客户端把它
    上传到 vault/evk.bin。优先用 vault 的 evk.bin(=字典),其次 vault/dictf,
    再回退目录内 'dictf' 前缀的最新文件(env AGENT_DICT_DIR → 包目录)。"""
    v = _vault_file("evk.bin") or _vault_file("dictf")
    if v:
        return v
    try:
        d = Path(_resolve_dict_dir())
        if d.is_dir():
            cands = [p for p in d.iterdir() if p.is_file() and p.name.startswith("dictf")]
            if cands:
                return str(max(cands, key=lambda p: p.stat().st_mtime))
    except OSError:
        pass
    return None


def _resolve_user_auth() -> str:
    env = os.environ.get("AGENT_USER_AUTH")
    if env:
        return env
    v = _vault_file("user_authorization")   # 从主机拉到 vault 的授权
    if v:
        return v
    d = _pkg_file_dir("henumpy")
    return str(d / "user_authorization") if d else _FALLBACK_USER_AUTH


def get_backend_from_env() -> Backend:
    """通过环境变量 AGENT_BACKEND=stub|real 选择 backend。"""
    val = os.environ.get("AGENT_BACKEND", "stub").lower()
    if val not in ("stub", "real"):
        raise ValueError(f"AGENT_BACKEND 只能是 stub 或 real,得到: {val}")
    return val  # type: ignore[return-value]


def _init_with_fd_capture(fn) -> str:
    """在 fd(1) 级捕获 fn() 期间原生库打印到 stdout 的内容并返回。
    捕获不可用/失败时仍正常执行 fn(),只是返回空串 —— 绝不影响初始化本身。"""
    import os
    import tempfile

    saved = None
    tf = None
    try:
        saved = os.dup(1)
        tf = tempfile.TemporaryFile(mode="w+")
        os.dup2(tf.fileno(), 1)
    except Exception:  # noqa: BLE001 —— 捕获不可用,直接裸跑
        saved = None
    try:
        fn()
    finally:
        text = ""
        if saved is not None:
            try:
                os.dup2(saved, 1)
                os.close(saved)
                tf.seek(0)
                text = tf.read()
            except Exception:  # noqa: BLE001
                text = ""
            finally:
                if tf is not None:
                    tf.close()
    return text


def _parse_license(text: str) -> dict:
    """从 initSK 的原生输出里解析授权信息('… N days left to expiration.')。"""
    import re
    m = re.search(r"(\d+)\s*days?\s*left", text or "")
    raw = next((ln.strip() for ln in (text or "").splitlines()
                if "expiration" in ln or "Serial" in ln), "")
    return {
        "days_left": int(m.group(1)) if m else None,
        "verified": ("verification succeeded" in (text or "")) or ("succeeded" in (text or "")),
        "raw": raw,
    }


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
        self._license: Optional[dict] = None   # HE 库授权信息(initSK 时捕获)

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
        sk = self.config.sk_path or _resolve_sk_path()
        if not _file_exists(sk):
            return False
        # henumpy initDict 需要字典文件,默认目录下可能要一组文件 —— MVP 简化为检查目录非空
        dict_dir = Path(self.config.dict_path or _resolve_dict_dir())
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
        sk_path = self.config.sk_path or _resolve_sk_path()
        if not _file_exists(sk_path):
            raise RuntimeError(
                f"真实 backend 需要 sk 密钥文件,但未找到: {sk_path}。"
                f"请按 PROVIDE_ME.md 把密钥放到对应位置。"
            )
        import crypto_toolkit as ct

        ct.initSK(skFilePath=sk_path)
        self._sk_initialized = True

    def license_status(self) -> dict:
        """HE 库授权状态(到期预警用)。需 initSK 已发生(否则 days_left 未知)。
        level: ok(>30d) / warn(≤30d) / critical(≤14d) / expired(≤0) / unknown。"""
        from datetime import date, timedelta
        lic = self._license
        if not lic or lic.get("days_left") is None:
            return {"available": False, "level": "unknown",
                    "message": "未捕获到授权信息(尚未初始化密钥,或捕获不可用)。"}
        d = int(lic["days_left"])
        level = "expired" if d <= 0 else "critical" if d <= 14 else "warn" if d <= 30 else "ok"
        expires = (date.today() + timedelta(days=d)).isoformat()
        msg = {
            "ok": f"HE 库授权正常,剩余 {d} 天(约 {expires} 到期)。",
            "warn": f"⚠ HE 库授权剩余 {d} 天(约 {expires} 到期),请及时续期。",
            "critical": f"⚠⚠ HE 库授权仅剩 {d} 天(约 {expires} 到期),尽快续期,否则密态将全部失效!",
            "expired": "❌ HE 库授权已过期,密态运算已失效,请立即续期。",
        }[level]
        return {"available": True, "days_left": d, "expires_on": expires,
                "level": level, "verified": lic.get("verified"),
                "raw": lic.get("raw"), "message": msg}

    def ensure_dict_initialized(self) -> None:
        """调用 hp.initDict(),只调一次。"""
        if self._dict_initialized:
            return
        dict_path = self.config.dict_path or _resolve_dict_file()
        user_auth = self.config.user_auth_path or _resolve_user_auth()
        if not _file_exists(user_auth):
            raise RuntimeError(
                "真实 backend 需要 user_authorization 文件,但未找到。"
                "请在客户端「密钥」面板点「从主机获取」拉取证书。"
            )
        if not (dict_path and _file_exists(dict_path)):
            raise RuntimeError(
                "真实 backend 需要字典文件(dictf),但未找到。"
                "请在客户端「密钥」面板上传字典文件(dictf)。"
            )
        import henumpy as hp

        # initDict 时原生库会向 stdout 打印授权信息("Serial number … N days left to expiration"),
        # 仅首次打印 → fd 级捕获并解析,供 license 到期预警。捕获失败绝不影响初始化。
        captured = _init_with_fd_capture(
            lambda: hp.initDict(dictFilePath=dict_path, userFilePath=user_auth))
        self._license = _parse_license(captured)
        self._dict_initialized = True

    def ensure_all_initialized(self) -> None:
        """计算/加密前的总开关 —— 调一次包揽 initSK + initDict。"""
        self.ensure_sk_initialized()
        self.ensure_dict_initialized()
