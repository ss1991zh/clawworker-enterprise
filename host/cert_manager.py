"""
A1 用户授权管理(architecture.md §A1)。

⚠️ 术语订正(v3):
- 架构里的"证书"在实际实现中就是 HE 工具链的 `user_authorization` 文件
  (henumpy initDict 加载,SDK 自带有效期 / 签名校验)
- 模型名保留为 `Certificate` 仅作历史兼容(类内部已是授权语义)
- 主机侧只跟踪"每个用户的 user_authorization 文件路径 + 账户状态",
  真正的有效期校验在客户端调 hp.initDict 时由 SDK 完成
- 客户端 init 失败 → 上报主机 → 主机把对应账户标记为 DISABLED

⚠️ 持久化:
- 证书副本文件本来就在 storage_root 下落盘
- 索引(username → 元数据)JSON 落到 storage_root / "index.json"
- 进程重启后 _load_index() 自动恢复
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


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
        self._index_path = self._storage_root / "index.json"
        self._lock = threading.Lock()
        self._auths: dict[str, UserAuthorization] = {}  # username → auth
        # 旧的 cert_id → cert 索引,保留兼容
        self._by_id: dict[str, UserAuthorization] = {}
        self._load_index()
        self._adopt_orphans()

    # ----- 持久化 -----
    def _load_index(self) -> None:
        """从磁盘恢复索引。索引损坏则忽略(空启动)。"""
        if not self._index_path.exists():
            return
        try:
            data = json.loads(self._index_path.read_text(encoding="utf-8"))
        except Exception:
            return
        for item in data:
            try:
                auth = UserAuthorization(
                    auth_id=item["auth_id"],
                    subject=item["subject"],
                    file_path=Path(item["file_path"]),
                    imported_at=datetime.fromisoformat(item["imported_at"]),
                    revoked=bool(item.get("revoked", False)),
                    sdk_init_failed_at=(
                        datetime.fromisoformat(item["sdk_init_failed_at"])
                        if item.get("sdk_init_failed_at") else None
                    ),
                )
                # 文件不在了:仍然恢复索引,但 is_valid() 会自动失败
                self._auths[auth.subject] = auth
                self._by_id[auth.auth_id] = auth
            except (KeyError, ValueError, TypeError):
                continue

    def _adopt_orphans(self) -> None:
        """
        启动期一次性迁移:扫描 storage_root 下所有 `<username>.<digest>.auth`,
        若不在内存索引里就回填一条(为旧版本无持久化时残留的文件兜底)。
        密码/账户无法恢复,这些会显示为"孤立证书",管理页可单独删除或重建账户。
        """
        adopted = 0
        for p in self._storage_root.glob("*.auth"):
            stem = p.name[:-5]  # 去掉 .auth
            if "." not in stem:
                continue
            username, digest = stem.rsplit(".", 1)
            if username in self._auths or digest in self._by_id:
                continue
            try:
                mtime = datetime.fromtimestamp(p.stat().st_mtime)
            except OSError:
                continue
            auth = UserAuthorization(
                auth_id=digest,
                subject=username,
                file_path=p,
                imported_at=mtime,
            )
            self._auths[username] = auth
            self._by_id[digest] = auth
            adopted += 1
        if adopted:
            self._save_index()

    def _save_index(self) -> None:
        data: list[dict[str, Any]] = []
        for auth in self._auths.values():
            data.append({
                "auth_id": auth.auth_id,
                "subject": auth.subject,
                "file_path": str(auth.file_path),
                "imported_at": auth.imported_at.isoformat(),
                "revoked": auth.revoked,
                "sdk_init_failed_at": (
                    auth.sdk_init_failed_at.isoformat()
                    if auth.sdk_init_failed_at else None
                ),
            })
        # 原子写入:先写 .tmp 再 rename
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._index_path)

    # ----- 导入(强制 1 证书 1 用户)-----
    def import_authorization(self, *, username: str, source: Path) -> UserAuthorization:
        """
        把外部平台签发的 user_authorization 文件导入主机存储,绑定给指定用户。

        1:1 强制规则:
        - 同一 username 不能重复导入(必须先删除旧用户)
        - 同一证书(SHA-256 指纹相同)不能被绑给不同用户
          (无论上传时换不换文件名,内容指纹一致即拒绝)
        """
        if not source.exists():
            raise FileNotFoundError(f"user_authorization 源文件不存在: {source}")

        if username in self._auths:
            raise ValueError(
                f"用户 {username} 已有授权;请先删除旧用户,或换一个用户名"
            )

        import hashlib

        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]

        # 关键校验:证书指纹查重(防止把同一份证书反复上传给多个用户)
        existing = self._by_id.get(digest)
        if existing:
            raise ValueError(
                f"⚠️ 证书重复:这份证书(指纹 {digest})已经分配给用户「{existing.subject}」。"
                f" 系统强制 1 张证书只能创建 1 个用户。"
                f" 请换一份新证书,或先到用户列表删除「{existing.subject}」释放该证书。"
            )

        dst = self._storage_root / f"{username}.{digest}.auth"
        dst.write_bytes(source.read_bytes())

        with self._lock:
            auth = UserAuthorization(
                auth_id=digest,
                subject=username,
                file_path=dst,
                imported_at=datetime.now(),
            )
            self._auths[username] = auth
            self._by_id[digest] = auth
            self._save_index()
            return auth

    # ----- 删除 -----
    def delete(self, username: str) -> bool:
        """
        彻底删除指定用户的授权(及主机侧存储副本)。
        删除后,同一证书可以再被导入(给新用户)。
        """
        with self._lock:
            auth = self._auths.pop(username, None)
            if not auth:
                return False
            self._by_id.pop(auth.auth_id, None)
            try:
                auth.file_path.unlink()
            except FileNotFoundError:
                pass
            self._save_index()
            return True

    # ----- 查询 -----
    def get_by_username(self, username: str) -> Optional[UserAuthorization]:
        return self._auths.get(username)

    def get(self, auth_id: str) -> Optional[UserAuthorization]:
        return self._by_id.get(auth_id)

    def list_active(self) -> list[UserAuthorization]:
        return [a for a in self._auths.values() if a.is_valid()]

    # ----- 状态变更 -----
    def revoke(self, username: str) -> None:
        with self._lock:
            if username in self._auths:
                self._auths[username].revoked = True
                self._save_index()

    def report_init_failed(self, username: str) -> None:
        """
        客户端调 hp.initDict 失败时上报。
        主机标记授权失效,后续登录会被拒绝。
        """
        with self._lock:
            auth = self._auths.get(username)
            if auth and auth.sdk_init_failed_at is None:
                auth.sdk_init_failed_at = datetime.now()
                self._save_index()


# 向后兼容:CertificateManager 名字仍可用
CertificateManager = AuthorizationManager
