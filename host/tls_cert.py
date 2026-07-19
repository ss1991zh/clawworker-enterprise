"""
主机自签 TLS 证书 —— 内网无公网 CA,自己签一张,客户端用指纹锁定信任(TOFU)。

- 首次需要时生成:SAN 覆盖本机所有 IPv4 + 主机名 + localhost,有效期 10 年。
- key/cert 落 ~/.agent-system/host-config/,私钥 ACL 收紧到仅属主。
- 指纹 = 证书 DER 的 SHA-256(冒号十六进制),供客户端锁定 + 双方核对。
- 已存在且本机 IP 未变则复用;IP 变了(SAN 不含新 IP)自动重签。
"""
from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from pathlib import Path
from typing import Optional

CERT_DIR = Path(os.path.expanduser("~/.agent-system/host-config"))
CERT_FILE = CERT_DIR / "host_cert.pem"
KEY_FILE = CERT_DIR / "host_key.pem"


def _local_ipv4() -> list[str]:
    """枚举本机 IPv4(用于证书 SAN)。至少含 127.0.0.1。"""
    ips: set[str] = {"127.0.0.1"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    # UDP 连一个不发包的外部地址,拿到出口网卡 IP(不真正建连)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("10.255.255.255", 1))
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except OSError:
        pass
    return sorted(ips)


def fingerprint(cert_pem_path: Path = CERT_FILE) -> str:
    """证书指纹:DER 的 SHA-256,大写冒号十六进制(与浏览器/openssl 一致)。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    data = cert_pem_path.read_bytes()
    cert = x509.load_pem_x509_certificate(data)
    fp = cert.fingerprint(hashes.SHA256())
    return ":".join(f"{b:02X}" for b in fp)


def _cert_covers_current_ips(cert_pem_path: Path) -> bool:
    """证书 SAN 是否已覆盖当前所有本机 IP(否则需重签)。"""
    from cryptography import x509
    try:
        cert = x509.load_pem_x509_certificate(cert_pem_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        cert_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
        return set(_local_ipv4()).issubset(cert_ips)
    except Exception:  # noqa: BLE001
        return False


def generate(cert_dir: Path = CERT_DIR) -> tuple[Path, Path, str]:
    """生成自签证书 + 私钥,返回 (cert_path, key_path, fingerprint)。"""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    import datetime as _dt

    cert_dir.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    hostname = socket.gethostname() or "clawworker-host"
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Clawworker Enterprise"),
    ])
    alt_names: list = [x509.DNSName("localhost"), x509.DNSName(hostname)]
    for ip in _local_ipv4():
        try:
            alt_names.append(x509.IPAddress(ipaddress.ip_address(ip)))
        except ValueError:
            pass

    # 用固定纪元避免依赖 datetime.now()(与本工程约束一致,不影响证书有效性)
    not_before = _dt.datetime(2020, 1, 1, tzinfo=_dt.timezone.utc)
    not_after = _dt.datetime(2035, 1, 1, tzinfo=_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject).issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before).not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path = cert_dir / "host_cert.pem"
    key_path = cert_dir / "host_key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    ))
    # 私钥 ACL 收紧到仅属主(复用 secret_store 的 icacls 加固)
    try:
        from host import secret_store
        secret_store.harden_file(key_path)
    except Exception:  # noqa: BLE001
        pass
    return cert_path, key_path, fingerprint(cert_path)


def ensure_cert(cert_dir: Path = CERT_DIR) -> tuple[Path, Path, str]:
    """
    确保证书就位:存在且覆盖当前 IP 则复用,否则(缺失/IP 变了)重新生成。
    返回 (cert_path, key_path, fingerprint)。cryptography 不可用时抛异常由调用方兜底。
    """
    cert_path = cert_dir / "host_cert.pem"
    key_path = cert_dir / "host_key.pem"
    if cert_path.exists() and key_path.exists() and _cert_covers_current_ips(cert_path):
        return cert_path, key_path, fingerprint(cert_path)
    return generate(cert_dir)


def current_fingerprint(cert_dir: Path = CERT_DIR) -> Optional[str]:
    """已有证书的指纹(供 admin 页展示);无证书返回 None。"""
    cert_path = cert_dir / "host_cert.pem"
    if not cert_path.exists():
        return None
    try:
        return fingerprint(cert_path)
    except Exception:  # noqa: BLE001
        return None
