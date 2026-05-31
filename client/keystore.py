"""
B1 密钥导入 / 本地隔离(architecture.md §B1)。

- 导入外部平台生成的 sk(解密密钥)、evk(计算密钥)
- 本地隔离:多用户共享同一台机器时,各自的密钥互不可见
- 永不出本机

⚠️ STUB:MVP 仅做文件路径管理 + 简单的目录权限。
   生产实现应集成 macOS Keychain / Secure Enclave。
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

DEFAULT_KEYSTORE_DIR = Path.home() / ".agent-system" / "keystore"


@dataclass
class KeyPaths:
    sk_path: Path
    evk_path: Path
    user_auth_path: Optional[Path] = None  # 每用户的 user_authorization 文件


class Keystore:
    """
    每用户独立目录,Unix 权限 0700 实现"本地隔离"。
    密钥与授权文件均不上传、不通过网络传输。
    """

    def __init__(self, root: Optional[Path] = None):
        self._root = root or DEFAULT_KEYSTORE_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        os.chmod(self._root, stat.S_IRWXU)

    def import_keys(
        self,
        *,
        username: str,
        sk_src: Path,
        evk_src: Path,
        user_auth_src: Optional[Path] = None,
    ) -> KeyPaths:
        """
        把外部平台生成的密钥 + user_authorization 文件导入本地隔离目录。
        """
        user_dir = self._root / username
        user_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(user_dir, stat.S_IRWXU)

        sk_dst = user_dir / "sk.bin"
        evk_dst = user_dir / "evk.bin"
        sk_dst.write_bytes(sk_src.read_bytes())
        evk_dst.write_bytes(evk_src.read_bytes())
        os.chmod(sk_dst, stat.S_IRUSR | stat.S_IWUSR)
        os.chmod(evk_dst, stat.S_IRUSR | stat.S_IWUSR)

        auth_dst: Optional[Path] = None
        if user_auth_src is not None:
            auth_dst = user_dir / "user_authorization"
            auth_dst.write_bytes(user_auth_src.read_bytes())
            os.chmod(auth_dst, stat.S_IRUSR | stat.S_IWUSR)

        return KeyPaths(sk_path=sk_dst, evk_path=evk_dst, user_auth_path=auth_dst)

    def import_user_authorization(self, *, username: str, source: Path) -> Path:
        """单独导入/替换某用户的 user_authorization 文件。"""
        user_dir = self._root / username
        user_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(user_dir, stat.S_IRWXU)
        dst = user_dir / "user_authorization"
        dst.write_bytes(source.read_bytes())
        os.chmod(dst, stat.S_IRUSR | stat.S_IWUSR)
        return dst

    def get_paths(self, username: str) -> Optional[KeyPaths]:
        user_dir = self._root / username
        sk = user_dir / "sk.bin"
        evk = user_dir / "evk.bin"
        if not sk.exists() or not evk.exists():
            return None
        auth = user_dir / "user_authorization"
        return KeyPaths(
            sk_path=sk,
            evk_path=evk,
            user_auth_path=auth if auth.exists() else None,
        )
