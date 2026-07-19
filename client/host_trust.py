"""
客户端对主机 TLS 证书的 TOFU(首次信任即锁定)信任管理。

内网主机是自签证书,没有公网 CA 背书。做法(同 SSH 模型):
- 首次连某主机:抓取它的证书,存为该主机的"锁定证书",记录指纹。
- 之后每次 httpx 用 verify=<锁定证书> —— OpenSSL 校验主机出示的正是这张(自签证书作
  自身信任锚)+ SAN 匹配地址。任何中间人换了证书都会校验失败。
- 证书轮换(主机重签)→ 校验失败,上层提示用户核对新指纹后 repin。

残留风险仅"首次连接时就有人中间人",用双方显示指纹供人工核对一次兜底。
"""
from __future__ import annotations

import hashlib
import os
import ssl
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse

_PIN_DIR = Path(os.path.expanduser("~/.agent-system/host-trust"))


class HostCertChanged(Exception):
    """主机证书与已锁定的不一致(疑似轮换或中间人)——需人工核对后 repin。"""
    def __init__(self, host_url: str, pinned_fp: str, seen_fp: str):
        super().__init__(f"主机 {host_url} 的证书指纹变了")
        self.host_url = host_url
        self.pinned_fp = pinned_fp
        self.seen_fp = seen_fp


def to_https(host_url: str) -> str:
    """把 host_url 规整为 https(主机端已启 TLS)。"""
    u = urlparse(host_url if "://" in host_url else "https://" + host_url)
    return urlunparse(u._replace(scheme="https"))


def _hostport(host_url: str) -> tuple[str, int]:
    u = urlparse(to_https(host_url))
    return (u.hostname or "127.0.0.1"), (u.port or 8443)


def _pin_file(host_url: str) -> Path:
    host, port = _hostport(host_url)
    safe = "".join(c if c.isalnum() else "_" for c in f"{host}_{port}")
    return _PIN_DIR / f"{safe}.pem"


def _fp_of_pem(pem: bytes) -> str:
    """证书 PEM → DER 的 SHA-256 指纹(大写冒号十六进制)。"""
    der = ssl.PEM_cert_to_DER_cert(pem.decode("ascii") if isinstance(pem, bytes) else pem)
    fp = hashlib.sha256(der).digest()
    return ":".join(f"{b:02X}" for b in fp)


def fetch_server_cert_pem(host_url: str, timeout: float = 8.0) -> bytes:
    """抓取主机当前出示的证书(不校验,仅用于 TOFU 首锁 / 指纹展示)。"""
    host, port = _hostport(host_url)
    pem = ssl.get_server_certificate((host, port), timeout=timeout)
    return pem.encode("ascii")


def pinned_fingerprint(host_url: str) -> Optional[str]:
    p = _pin_file(host_url)
    if not p.exists():
        return None
    try:
        return _fp_of_pem(p.read_bytes())
    except Exception:  # noqa: BLE001
        return None


def server_fingerprint(host_url: str) -> Optional[str]:
    try:
        return _fp_of_pem(fetch_server_cert_pem(host_url))
    except Exception:  # noqa: BLE001
        return None


def repin(host_url: str) -> str:
    """(重新)锁定主机当前证书 —— 首次登记或证书轮换后由用户确认调用。返回新指纹。"""
    _PIN_DIR.mkdir(parents=True, exist_ok=True)
    pem = fetch_server_cert_pem(host_url)
    p = _pin_file(host_url)
    p.write_bytes(pem)
    return _fp_of_pem(pem)


def verify_for(host_url: str) -> str:
    """
    返回给 httpx verify= 用的锁定证书路径。
    - 已锁定:直接返回(httpx 会校验主机出示的正是这张 + SAN 匹配)。
    - 未锁定:TOFU 首次抓取并锁定(记录指纹),返回。
    抓不到证书(主机没起/网络问题)则抛异常由上层处理。
    """
    p = _pin_file(host_url)
    if p.exists():
        return str(p)
    repin(host_url)
    return str(p)


def check_cert_unchanged(host_url: str) -> None:
    """主动核对主机当前证书是否与锁定一致;变了则抛 HostCertChanged。"""
    pinned = pinned_fingerprint(host_url)
    if pinned is None:
        return
    seen = server_fingerprint(host_url)
    if seen and seen != pinned:
        raise HostCertChanged(host_url, pinned, seen)
