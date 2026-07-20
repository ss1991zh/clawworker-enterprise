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


def _spki_of_pem(pem: bytes) -> Optional[str]:
    """证书里**公钥**(SubjectPublicKeyInfo)的 SHA-256 指纹。

    主机换网络后本机 IP 变,证书 SAN 要更新 → 必须重签,整证书指纹随之改变。
    但主机重签时复用私钥,**公钥不变** —— 以 SPKI 判断"还是不是原来那台主机",
    可以在合法重签时不误报中间人(否则演示途中换个 WiFi 就被红屏拦住)。
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        cert = x509.load_pem_x509_certificate(pem)
        der = cert.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        return ":".join(f"{b:02X}" for b in hashlib.sha256(der).digest())
    except Exception:  # noqa: BLE001 —— 解析不了就当拿不到 SPKI,由调用方按"变了"处理
        return None


def pinned_spki(host_url: str) -> Optional[str]:
    p = _pin_file(host_url)
    return _spki_of_pem(p.read_bytes()) if p.exists() else None


def server_spki(host_url: str) -> Optional[str]:
    try:
        return _spki_of_pem(fetch_server_cert_pem(host_url))
    except Exception:  # noqa: BLE001
        return None


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


def heal_if_same_host(host_url: str) -> bool:
    """TLS 校验失败时的自愈:若主机**公钥没变**(只是换网重签),续锁新证书并返回 True。

    返回 True 表示"确认还是原来那台主机,已续锁,可以重试";
    返回 False 表示公钥变了/拿不到 —— 交给上层按"疑似中间人"提示人工核对。
    """
    p_spki = pinned_spki(host_url)
    if not p_spki:
        return False
    if server_spki(host_url) != p_spki:
        return False
    try:
        _pin_file(host_url).write_bytes(fetch_server_cert_pem(host_url))
        return True
    except Exception:  # noqa: BLE001
        return False


def check_cert_unchanged(host_url: str) -> None:
    """主动核对主机身份是否与锁定一致;换了主机才抛 HostCertChanged。

    判据是**公钥(SPKI)**而不是整张证书:主机换网络后 IP 变、SAN 要更新 → 合法重签,
    整证书指纹必变但公钥不变。此时自动把 pin 更新成新证书(身份没变,只是换了张皮),
    不打断使用;只有**公钥也变了**才说明换了主机/有人中间人,才拦截并要人工核对。
    """
    pinned = pinned_fingerprint(host_url)
    if pinned is None:
        return
    seen = server_fingerprint(host_url)
    if not seen or seen == pinned:
        return

    p_spki, s_spki = pinned_spki(host_url), server_spki(host_url)
    if p_spki and s_spki and p_spki == s_spki:
        # 同一把私钥签出来的新证书 —— 合法重签,静默续锁
        try:
            _pin_file(host_url).write_bytes(fetch_server_cert_pem(host_url))
        except Exception:  # noqa: BLE001 —— 刷新失败不升级为"疑似中间人"
            pass
        return
    raise HostCertChanged(host_url, pinned, seen)
