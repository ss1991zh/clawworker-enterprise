"""
A1 用户授权管理(architecture.md §A1)。

⚠️ 术语订正(v3):
- 架构里的"证书"在实际实现中就是 HE 工具链的 `user_authorization` 文件
  (henumpy initDict 加载,SDK 自带有效期 / 签名校验)
- 模型名保留为 `Certificate` 仅作历史兼容(类内部已是授权语义)
- 主机侧只跟踪"每个用户的 user_authorization 文件路径 + 账户状态",
  真正的有效期校验在客户端调 hp.initDict 时由 SDK 完成
- 客户端 init 失败 → 上报主机 → 主机把对应账户标记为 DISABLED
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional


@dataclass
class UserAuthorization:
    """
    用户授权 —— 对应 user_authorization 文件。

    每个 username 绑定一份独立的授权文件(per-user 模式)。
    """

    auth_id: str  # 唯一 ID(可用文件 SHA-256 前 12 位)
    subject: str  # 主体标识(username)
    file_path: Path  # 主机侧保存的副本路径
    imported_at: datetime
    revoked: bool = False
    # SDK 自带有效期,主机不解析,只在客户端 init 失败时被动标记
    sdk_init_failed_at: Optional[datetime] = None

    def is_valid(self) -> bool:
        """主机侧"软"判定:文件存在且未被吊销 / 未被标记失效。"""
        if self.revoked:
            return False
        if self.sdk_init_failed_at is not None:
            return False
        if not self.file_path.exists():
            return False
        return True


# 向后兼容别名:旧代码引用的 Certificate / CertificateInfo / CertificateManager
CertificateInfo = UserAuthorization


class AuthorizationManager:
    """
    用户授权库 —— 管理每用户一份的 user_authorization 文件。

    主机侧职责:
    - 导入授权文件(复制到本地存储或仅记录路径)
    - 维护 username → UserAuthorization 索引
    - 接收客户端"init 失败"上报后,将对应授权标记为失效
    - 提供"账户是否仍可用"的查询接口
    """

    def __init__(self, storage_root: Optional[Path] = None):
        self._storage_root = storage_root or (Path.home() / ".agent-system" / "host-auth")
        self._storage_root.mkdir(parents=True, exist_ok=True)
        self._auths: dict[str, UserAuthorization] = {}  # username → auth
        # 旧的 cert_id → cert 索引,保留兼容
        self._by_id: dict[str, UserAuthorization] = {}

    # ----- 导入 -----
    def import_authorization(self, *, username: str, source: Path) -> UserAuthorization:
        """
        把外部平台签发的 user_authorization 文件导入主机存储,绑定给指定用户。

        Args:
            username: 账户标识
            source: 外部 user_authorization 文件路径

        Returns:
            UserAuthorization 实例
        """
        if not source.exists():
            raise FileNotFoundError(f"user_authorization 源文件不存在: {source}")

        # 计算文件指纹作为 auth_id
        import hashlib

        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]

        # 复制到主机存储,文件名按用户名 + 指纹
        dst = self._storage_root / f"{username}.{digest}.auth"
        dst.write_bytes(source.read_bytes())

        auth = UserAuthorization(
            auth_id=digest,
            subject=username,
            file_path=dst,
            imported_at=datetime.now(),
        )
        self._auths[username] = auth
        self._by_id[digest] = auth
        return auth

    # ----- 查询 -----
    def get_by_username(self, username: str) -> Optional[UserAuthorization]:
        return self._auths.get(username)

    def get(self, auth_id: str) -> Optional[UserAuthorization]:
        return self._by_id.get(auth_id)

    def list_active(self) -> list[UserAuthorization]:
        return [a for a in self._auths.values() if a.is_valid()]

    # ----- 状态变更 -----
    def revoke(self, username: str) -> None:
        if username in self._auths:
            self._auths[username].revoked = True

    def report_init_failed(self, username: str) -> None:
        """
        客户端调 hp.initDict 失败时上报。
        主机标记授权失效,后续登录会被拒绝。
        """
        auth = self._auths.get(username)
        if auth and auth.sdk_init_failed_at is None:
            auth.sdk_init_failed_at = datetime.now()


# 向后兼容:CertificateManager 名字仍可用
CertificateManager = AuthorizationManager
