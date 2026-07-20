"""
内网扫描发现主机 —— 免去手输 IP(演示时主机 IP 常随网络变化)。

安全前提(重要):**扫到的主机默认不可信**。同一内网里任何人都能在 8443 上跑一个
假主机;若客户端扫到就直接信任,等于把账号口令和 token 送出去 —— 比手输 IP 更危险。
因此本模块只做"找出候选 + 算出各自公钥指纹(SPKI)",信任决策交给上层:

  · 已 pin 过的客户端:挑 SPKI 与本地锁定一致的那台 → 密码学安全,可零交互直连
    (攻击者拿不到主机私钥,伪造不出相同公钥)
  · 首次配置:必须由人对照管理端显示的指纹核对后选定 —— 这一步是 TOFU 的信任锚,
    不能因为"扫描到了"就省略

范围限制:只扫本机所在的 /24(254 个地址,并发下 1~3 秒)。真实子网更大(如 /16)时
扫不全,由上层提示改用手动输入 —— 不做全网段扫描,既不现实也会触发企业 EDR 告警。
"""
from __future__ import annotations

import ipaddress
import socket
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

HOST_PORT = 8443
_CONNECT_TIMEOUT = 0.35      # 单地址 TCP 探测超时(内网 RTT 很低,给 350ms 足够)
_MAX_WORKERS = 64
# 主机自签证书的固定 CN(见 host/tls_cert.py)—— 只用于**过滤**非本产品的服务,
# 不作为安全判据(CN 可伪造;真正的身份判据是 SPKI)
_HOST_CN = "Clawworker Enterprise Local"


def local_ipv4() -> list[str]:
    """本机非回环 IPv4。"""
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in out:
                out.append(ip)
    except Exception:  # noqa: BLE001
        pass
    return out


def candidate_networks() -> list[str]:
    """本机所在的 /24 网段(去重)。只取私网地址。"""
    nets: list[str] = []
    for ip in local_ipv4():
        try:
            addr = ipaddress.ip_address(ip)
            if not addr.is_private or addr.is_link_local:
                continue
            net = str(ipaddress.ip_network(f"{ip}/24", strict=False))
            if net not in nets:
                nets.append(net)
        except ValueError:
            continue
    return nets


def _port_open(ip: str, port: int = HOST_PORT) -> bool:
    try:
        with socket.create_connection((ip, port), timeout=_CONNECT_TIMEOUT):
            return True
    except OSError:
        return False


def _probe(ip: str) -> Optional[dict]:
    """端口通就取证书,返回 {ip, url, spki, cn};不是本产品/取不到证书返回 None。"""
    if not _port_open(ip):
        return None
    url = f"https://{ip}:{HOST_PORT}"
    try:
        from client import host_trust
        pem = host_trust.fetch_server_cert_pem(url)
        spki = host_trust._spki_of_pem(pem)
        if not spki:
            return None
        cn = _cert_cn(pem)
        if cn and _HOST_CN not in cn:
            return None          # 8443 上跑着别的服务,不是本产品
        return {"ip": ip, "url": url, "spki": spki, "cn": cn or ""}
    except Exception:  # noqa: BLE001 —— 取证书失败视为不可用候选
        return None


def _cert_cn(pem: bytes) -> str:
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        cert = x509.load_pem_x509_certificate(pem)
        vals = cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        return vals[0].value if vals else ""
    except Exception:  # noqa: BLE001
        return ""


def scan(network: Optional[str] = None) -> dict:
    """扫描内网找主机。

    返回 {networks: [...], candidates: [{ip,url,spki,cn,known}], too_large: bool}
    known=True 表示该主机的公钥与本地已锁定的一致 —— 可安全直连,无需人工核对。
    """
    nets = [network] if network else candidate_networks()
    if not nets:
        return {"networks": [], "candidates": [], "too_large": False,
                "message": "未找到本机内网地址(可能未连接网络)"}

    targets: list[str] = []
    too_large = False
    for n in nets:
        try:
            net = ipaddress.ip_network(n, strict=False)
        except ValueError:
            continue
        if net.num_addresses > 256:      # 只扫 /24 及更小,大网段不扫(不现实且像端口扫描)
            too_large = True
            continue
        targets.extend(str(h) for h in net.hosts())

    found: list[dict] = []
    if targets:
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as ex:
            for r in ex.map(_probe, targets):
                if r:
                    found.append(r)

    known = known_spkis()
    for c in found:
        c["known"] = c["spki"] in known
    # 已认识的排前面
    found.sort(key=lambda c: (not c["known"], c["ip"]))
    return {"networks": nets, "candidates": found, "too_large": too_large}


def known_spkis() -> set[str]:
    """本地所有已锁定主机的公钥指纹集合。"""
    from client import host_trust
    out: set[str] = set()
    try:
        for p in host_trust._PIN_DIR.glob("*.pem"):
            spki = host_trust._spki_of_pem(p.read_bytes())
            if spki:
                out.add(spki)
    except Exception:  # noqa: BLE001
        pass
    return out
