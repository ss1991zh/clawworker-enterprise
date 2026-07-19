"""
敏感字符串的静态加密(at-rest)—— 用于 LLM API Key 等不该明文落盘的配置。

Windows:调用系统 DPAPI(CryptProtectData/CryptUnprotectData,经 ctypes,无需第三方包)
        —— 密文与**当前用户账户**绑定,别的本地用户/拷走文件都解不开,无需自管密钥。
其它平台:暂无等价的免依赖方案 → 原样返回并打标记,靠文件 ACL(0600)兜底(诚实降级)。

存储格式:protect() 产出 "dpapi:<base64>";unprotect() 只解带该前缀的值,
其余(历史明文或降级明文)原样返回,保证向后兼容与可读性。
"""
from __future__ import annotations

import base64
import ctypes
import os
from ctypes import wintypes

_PREFIX = "dpapi:"


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> "_DATA_BLOB":
    buf = ctypes.create_string_buffer(data, len(data))
    return _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))


def _blob_bytes(blob: "_DATA_BLOB") -> bytes:
    n = int(blob.cbData)
    out = ctypes.string_at(blob.pbData, n)
    ctypes.windll.kernel32.LocalFree(blob.pbData)
    return out


# 应用级 entropy:绑定到本应用 + 本次安装 —— 同用户下的通用木马即便调 DPAPI
# 也解不开(它不知道这份 entropy)。= 固定 app 常量 + 每安装随机密钥(ACL 保护的边文件)。
_APP_ENTROPY_CONST = b"clawworker-enterprise/llm-key/v1"
_ENTROPY_FILE = os.path.join(os.path.expanduser("~"), ".agent-system",
                             "host-config", ".secret_entropy")
_entropy_cache: bytes | None = None


def _entropy() -> bytes:
    global _entropy_cache
    if _entropy_cache is not None:
        return _entropy_cache
    secret = b""
    try:
        if os.path.exists(_ENTROPY_FILE):
            with open(_ENTROPY_FILE, "rb") as f:
                secret = f.read()
        if len(secret) < 16:
            os.makedirs(os.path.dirname(_ENTROPY_FILE), exist_ok=True)
            secret = os.urandom(32)
            with open(_ENTROPY_FILE, "wb") as f:
                f.write(secret)
            harden_file(_ENTROPY_FILE)     # 边密钥文件收紧 ACL 到仅属主
    except Exception:  # noqa: BLE001 —— 拿不到 install 密钥则仅用 app 常量(仍强于无 entropy)
        secret = b""
    _entropy_cache = _APP_ENTROPY_CONST + secret
    return _entropy_cache


def _dpapi(data: bytes, encrypt: bool, use_entropy: bool = True) -> bytes:
    fn = (ctypes.windll.crypt32.CryptProtectData if encrypt
          else ctypes.windll.crypt32.CryptUnprotectData)
    inp = _blob(data)
    ent_ptr = None
    if use_entropy:
        ent = _blob(_entropy())      # pOptionalEntropy:加解密必须一致
        ent_ptr = ctypes.byref(ent)
    out = _DATA_BLOB()
    # flags=1 → CRYPTPROTECT_UI_FORBIDDEN(不弹 UI,适合服务/后台)
    ok = fn(ctypes.byref(inp), None, ent_ptr, None, None, 1, ctypes.byref(out))
    if not ok:
        raise OSError("DPAPI 调用失败")
    return _blob_bytes(out)


def protect(plaintext: str) -> str:
    """加密敏感串。失败/非 Windows → 原样返回(靠 ACL 兜底)。"""
    if not plaintext or os.name != "nt":
        return plaintext
    try:
        enc = _dpapi(plaintext.encode("utf-8"), encrypt=True)
        return _PREFIX + base64.b64encode(enc).decode("ascii")
    except Exception:  # noqa: BLE001
        return plaintext


def unprotect(stored: str) -> str:
    """解密 protect() 产出的值;非本方案前缀的值原样返回(兼容历史明文)。"""
    if not stored or not stored.startswith(_PREFIX):
        return stored
    raw = None
    try:
        raw = base64.b64decode(stored[len(_PREFIX):])
        return _dpapi(raw, encrypt=False, use_entropy=True).decode("utf-8")
    except Exception:  # noqa: BLE001
        # 兼容:本改动之前的密文是**无 entropy** 加密的,回退再试一次
        if raw is not None:
            try:
                return _dpapi(raw, encrypt=False, use_entropy=False).decode("utf-8")
            except Exception:  # noqa: BLE001
                pass
        return stored


def is_protected(stored: str) -> bool:
    return bool(stored) and stored.startswith(_PREFIX)


def harden_file(path) -> bool:
    """把文件权限收紧到仅当前用户可读(与密钥沙盒同法);best-effort。"""
    import subprocess
    try:
        if os.name != "nt":
            os.chmod(path, 0o600)
            return True
        user = os.environ.get("USERNAME") or ""
        domain = os.environ.get("USERDOMAIN") or ""
        principal = f"{domain}\\{user}" if domain and user else user
        if not principal:
            return False
        r = subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
                           capture_output=True, text=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:  # noqa: BLE001
        return False
