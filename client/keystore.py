"""
B1 密钥导入 / 本地隔离(architecture.md §B1)。

- 导入外部平台生成的 sk(解密密钥)、evk(计算密钥)
- 本地隔离:多用户共享同一台机器时,各自的密钥互不可见
- 永不出本机

沙盒(MVP):
- 主目录 ~/.agent-system/keystore/ 0700(仅当前 macOS 用户可读)
- 每用户子目录 keystore/<username>/vault/ 0700
- 单个密钥文件 0600
- 路径解析强制 resolve(),拒绝 symlink 跳出沙盒
- 生产实现应集成 macOS Keychain / Secure Enclave

证书 user_authorization 的语义升级:
- 不再让用户在客户端 UI 上传(避免泄露)
- 客户端登录后通过 GET /auth/user_authorization 自动从主机拉一份"沙盒副本"
- admin 端是单一可信来源,吊销 → 客户端 init 即失败
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_KEYSTORE_DIR = Path.home() / ".agent-system" / "keystore"
VAULT_SUBDIR = "vault"   # 每用户子目录:keystore/<username>/vault/


def _harden_acl(path: Path) -> bool:
    """
    收紧文件/目录权限,让同机其他本地用户无法读取密钥。
    - 非 Windows:os.chmod 0700/0600(POSIX 权限位真实生效)。
    - Windows:os.chmod 只改只读位、**不设 NTFS ACL**,故改用 icacls
      去继承 + 只授当前用户 —— 否则 sk.bin 对同机其他账户可读。
    返回 True 表示已应用了有效的属主独占权限。
    """
    import subprocess
    try:
        if os.name != "nt":
            os.chmod(path, stat.S_IRWXU if path.is_dir() else (stat.S_IRUSR | stat.S_IWUSR))
            return True
        # Windows:去除继承的 ACE,只保留当前用户完全控制
        user = os.environ.get("USERNAME") or ""
        domain = os.environ.get("USERDOMAIN") or ""
        principal = f"{domain}\\{user}" if domain and user else (user or "")
        if not principal:
            return False
        r = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:(OI)(CI)F"]
            if path.is_dir() else
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{principal}:F"],
            capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.returncode == 0
    except Exception:  # noqa: BLE001 —— 加固失败不阻断功能,但由 sandbox_audit 如实上报
        return False


def _path_owner_only(path: Path) -> bool:
    """该路径是否只有属主可访问(POSIX 看权限位;Windows 看 NTFS ACL)。"""
    import subprocess
    try:
        if os.name != "nt":
            mode = stat.S_IMODE(path.stat().st_mode)
            return mode & 0o077 == 0            # 组/其他无任何权限
        r = subprocess.run(["icacls", str(path)], capture_output=True, text=True,
                           timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return False
        import re as _re
        # 只看真正的 ACE 授权(主体名紧跟 ":(权限)"),避免把路径里的 C:\Users\ 误当授权。
        # 授权给下列宽泛主体即视为未独占(覆盖中英文系统组名)。
        broad = r"(Everyone|Authenticated Users|BUILTIN\\Users|\bUsers|所有人|Todos)"
        return not _re.search(broad + r"\s*:\s*\(", r.stdout)
    except Exception:  # noqa: BLE001
        return False


@dataclass
class KeyPaths:
    sk_path: Path
    evk_path: Path
    user_auth_path: Optional[Path] = None  # 每用户的 user_authorization 文件
    dict_path: Optional[Path] = None        # 每用户的 henumpy 字典文件(dictf)


class Keystore:
    """
    每用户独立目录,Unix 权限 0700 实现"本地隔离"。
    密钥与授权文件均不上传、不通过网络传输。
    实际存储路径:
        ~/.agent-system/keystore/<username>/vault/{sk.bin, evk.bin, user_authorization}
    旧版本兼容:若 vault/ 不存在但 <username>/ 直接放着密钥,自动迁移。
    """

    def __init__(self, root: Optional[Path] = None):
        self._root = root or DEFAULT_KEYSTORE_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        _harden_acl(self._root)

    # ----- 沙盒辅助 -----
    def _vault_dir(self, username: str) -> Path:
        """返回 keystore/<username>/vault/,自动 0700。"""
        user_dir = self._root / username
        user_dir.mkdir(parents=True, exist_ok=True)
        _harden_acl(user_dir)
        vault = user_dir / VAULT_SUBDIR
        vault.mkdir(parents=True, exist_ok=True)
        _harden_acl(vault)
        # 兼容迁移:老版本把密钥直接放 user_dir,新版本搬进 vault/
        for legacy_name in ("sk.bin", "evk.bin", "user_authorization"):
            legacy = user_dir / legacy_name
            new = vault / legacy_name
            if legacy.exists() and not new.exists():
                try:
                    legacy.replace(new)
                    _harden_acl(new)
                except Exception:
                    pass
        return vault

    def _write_secret(self, dst: Path, data: bytes) -> None:
        """原子写 + 0600 权限。"""
        # 先写临时再 rename,避免半成品被读到
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        tmp.write_bytes(data)
        _harden_acl(tmp)
        tmp.replace(dst)
        _harden_acl(dst)

    def _resolve_in_sandbox(self, p: Path) -> Path:
        """解析后必须落在 self._root 内,否则视为攻击。"""
        rp = p.resolve()
        if not rp.is_relative_to(self._root.resolve()):
            raise PermissionError(f"路径跳出沙盒: {rp}")
        return rp

    # ----- 导入 / 替换 -----
    def import_keys(
        self,
        *,
        username: str,
        sk_src: Path,
        evk_src: Path,
        user_auth_src: Optional[Path] = None,
    ) -> KeyPaths:
        """把外部平台生成的密钥 + user_authorization 文件导入本地沙盒。"""
        vault = self._vault_dir(username)
        sk_dst = vault / "sk.bin"
        evk_dst = vault / "evk.bin"
        self._write_secret(sk_dst, sk_src.read_bytes())
        self._write_secret(evk_dst, evk_src.read_bytes())

        auth_dst: Optional[Path] = None
        if user_auth_src is not None:
            auth_dst = vault / "user_authorization"
            self._write_secret(auth_dst, user_auth_src.read_bytes())

        return KeyPaths(sk_path=sk_dst, evk_path=evk_dst, user_auth_path=auth_dst)

    def import_sk(self, *, username: str, source: Path) -> Path:
        """单独导入 / 替换 sk(解密密钥)。"""
        dst = self._vault_dir(username) / "sk.bin"
        self._write_secret(dst, source.read_bytes())
        return dst

    def import_evk(self, *, username: str, source: Path) -> Path:
        """单独导入 / 替换 evk(计算密钥)。"""
        dst = self._vault_dir(username) / "evk.bin"
        self._write_secret(dst, source.read_bytes())
        return dst

    def import_user_authorization(self, *, username: str, source: Path) -> Path:
        """单独导入/替换 user_authorization 文件(客户端 fetch_auth 用此路径)。"""
        dst = self._vault_dir(username) / "user_authorization"
        self._write_secret(dst, source.read_bytes())
        return dst

    def import_dict(self, *, username: str, source: Path) -> Path:
        """单独导入/替换 henumpy 字典文件(dictf)。引擎 initDict 用此路径。"""
        dst = self._vault_dir(username) / "dictf"
        self._write_secret(dst, source.read_bytes())
        return dst

    # ----- 查询 -----
    def get_paths(self, username: str) -> Optional[KeyPaths]:
        vault = self._vault_dir(username)
        sk = vault / "sk.bin"
        # 本方案真实初始化只需 sk + 字典 + 授权,不使用单独的 evk;
        # 因此只要 sk 在就返回(evk/字典/授权按存在性可选填)。
        if not sk.exists():
            return None
        evk = vault / "evk.bin"
        dictf = vault / "dictf"
        auth = vault / "user_authorization"
        return KeyPaths(
            sk_path=sk,
            evk_path=evk,
            user_auth_path=auth if auth.exists() else None,
            dict_path=dictf if dictf.exists() else None,
        )

    def vault_path(self, username: str) -> Path:
        """暴露给 UI 显示沙盒目录(展示用,不导出文件)。"""
        return self._vault_dir(username)

    def sandbox_audit(self, username: str) -> dict:
        """
        返回当前沙盒的健康检查报告(供 UI 展示)。
        跨平台如实判定属主独占:POSIX 看权限位 == 0o700/0o600;Windows 看 NTFS ACL
        (icacls 输出里不得授权给 Users/Everyone/Authenticated Users)——
        不再用无意义的 POSIX 位在 Windows 上给出虚假合规结论。
        """
        vault = self._vault_dir(username)
        root_ok = _path_owner_only(self._root)
        vault_ok = _path_owner_only(vault)
        files = {}
        files_ok = True
        for name in ("sk.bin", "evk.bin", "user_authorization"):
            p = vault / name
            if p.exists():
                locked = _path_owner_only(p)
                files_ok = files_ok and locked
                files[name] = {"present": True, "owner_only": locked,
                               "size_bytes": p.stat().st_size}
            else:
                files[name] = {"present": False}
        return {
            "root": str(self._root),
            "vault": str(vault),
            "platform": os.name,
            "root_ok": root_ok,
            "vault_ok": vault_ok,
            "files_ok": files_ok,
            "files": files,
        }
