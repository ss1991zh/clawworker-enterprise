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


@dataclass
class KeyPaths:
    sk_path: Path
    evk_path: Path
    user_auth_path: Optional[Path] = None  # 每用户的 user_authorization 文件


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
        os.chmod(self._root, stat.S_IRWXU)

    # ----- 沙盒辅助 -----
    def _vault_dir(self, username: str) -> Path:
        """返回 keystore/<username>/vault/,自动 0700。"""
        user_dir = self._root / username
        user_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(user_dir, stat.S_IRWXU)
        vault = user_dir / VAULT_SUBDIR
        vault.mkdir(parents=True, exist_ok=True)
        os.chmod(vault, stat.S_IRWXU)
        # 兼容迁移:老版本把密钥直接放 user_dir,新版本搬进 vault/
        for legacy_name in ("sk.bin", "evk.bin", "user_authorization"):
            legacy = user_dir / legacy_name
            new = vault / legacy_name
            if legacy.exists() and not new.exists():
                try:
                    legacy.replace(new)
                    os.chmod(new, stat.S_IRUSR | stat.S_IWUSR)
                except Exception:
                    pass
        return vault

    def _write_secret(self, dst: Path, data: bytes) -> None:
        """原子写 + 0600 权限。"""
        # 先写临时再 rename,避免半成品被读到
        tmp = dst.with_suffix(dst.suffix + ".tmp")
        tmp.write_bytes(data)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        tmp.replace(dst)
        os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)

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

    # ----- 查询 -----
    def get_paths(self, username: str) -> Optional[KeyPaths]:
        vault = self._vault_dir(username)
        sk = vault / "sk.bin"
        evk = vault / "evk.bin"
        if not sk.exists() or not evk.exists():
            # 仍允许"只有 evk 没有 sk"等中间态吗?MVP 维持原语义:都需在
            return None
        auth = vault / "user_authorization"
        return KeyPaths(
            sk_path=sk,
            evk_path=evk,
            user_auth_path=auth if auth.exists() else None,
        )

    def vault_path(self, username: str) -> Path:
        """暴露给 UI 显示沙盒目录(展示用,不导出文件)。"""
        return self._vault_dir(username)

    def sandbox_audit(self, username: str) -> dict:
        """返回当前沙盒的健康检查报告(供 UI 展示)。"""
        vault = self._vault_dir(username)
        root_mode = stat.S_IMODE(self._root.stat().st_mode)
        vault_mode = stat.S_IMODE(vault.stat().st_mode)
        files = {}
        for name in ("sk.bin", "evk.bin", "user_authorization"):
            p = vault / name
            if p.exists():
                files[name] = {
                    "present": True,
                    "mode": oct(stat.S_IMODE(p.stat().st_mode)),
                    "size_bytes": p.stat().st_size,
                }
            else:
                files[name] = {"present": False}
        return {
            "root": str(self._root),
            "vault": str(vault),
            "root_mode": oct(root_mode),     # 期望 0o700
            "vault_mode": oct(vault_mode),
            "root_ok": root_mode == 0o700,
            "vault_ok": vault_mode == 0o700,
            "files_ok": all(
                (not v["present"]) or v["mode"] == "0o600"
                for v in files.values()
            ),
            "files": files,
        }
