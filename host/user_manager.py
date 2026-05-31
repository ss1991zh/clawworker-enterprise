"""
A2 用户管理(architecture.md §A2)。

- 证书 = 创建账户(一证一账户)
- 账户状态:可用 / 禁用
- 登录:账号 + 密码(在证书有效的前提下)
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Optional


class AccountStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


@dataclass
class Account:
    username: str
    auth_id: str  # 关联的 user_authorization id(原 cert_id,术语统一)
    password_hash: str
    password_salt: str
    status: AccountStatus = AccountStatus.ACTIVE
    created_at: datetime = field(default_factory=datetime.now)

    # 兼容旧字段
    @property
    def cert_id(self) -> str:
        return self.auth_id


@dataclass
class Session:
    token: str
    username: str
    expires_at: datetime


def _hash_password(password: str, salt: str) -> str:
    """PBKDF2-HMAC-SHA256,200000 轮。"""
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 200_000)
    return dk.hex()


class UserManager:
    """用户与会话管理。"""

    SESSION_TTL = timedelta(hours=8)

    def __init__(self, auth_manager):
        """
        Args:
            auth_manager: AuthorizationManager(原 CertificateManager,术语统一)
        """
        self._auth_manager = auth_manager
        self._accounts: dict[str, Account] = {}  # username -> account
        self._sessions: dict[str, Session] = {}  # token -> session

    # 兼容旧名
    @property
    def _cert_manager(self):
        return self._auth_manager

    # ----- 账户管理 -----
    def create_account(self, *, username: str, password: str, auth_id: Optional[str] = None, cert_id: Optional[str] = None) -> Account:
        """
        用户授权激活账户 + 设密码。

        Args:
            username: 账户标识
            password: 密码
            auth_id: 关联的 user_authorization id;若不传则查找已绑定到该 username 的授权
            cert_id: 旧名兼容,等同 auth_id
        """
        if username in self._accounts:
            raise ValueError(f"账户 {username} 已存在")

        # auth 来源:显式 auth_id/cert_id,或按 username 查
        aid = auth_id or cert_id
        if aid:
            auth = self._auth_manager.get(aid)
        else:
            auth = self._auth_manager.get_by_username(username)

        if not auth or not auth.is_valid():
            raise ValueError("user_authorization 不存在或已失效")

        salt = secrets.token_hex(16)
        acct = Account(
            username=username,
            auth_id=auth.auth_id,
            password_hash=_hash_password(password, salt),
            password_salt=salt,
        )
        self._accounts[username] = acct
        return acct

    def disable(self, username: str) -> None:
        if username in self._accounts:
            self._accounts[username].status = AccountStatus.DISABLED

    def enable(self, username: str) -> None:
        if username in self._accounts:
            self._accounts[username].status = AccountStatus.ACTIVE

    # ----- 登录与会话 -----
    def login(self, *, username: str, password: str) -> Session:
        acct = self._accounts.get(username)
        if not acct:
            raise PermissionError("账户不存在")
        if acct.status != AccountStatus.ACTIVE:
            raise PermissionError("账户已禁用")

        auth = self._auth_manager.get(acct.auth_id)
        if not auth or not auth.is_valid():
            # user_authorization 失效 → 账户失效(architecture.md §A1 决策 #3)
            self.disable(username)
            raise PermissionError("user_authorization 已失效,账户被自动禁用")

        if _hash_password(password, acct.password_salt) != acct.password_hash:
            raise PermissionError("密码错误")

        token = secrets.token_urlsafe(32)
        sess = Session(
            token=token,
            username=username,
            expires_at=datetime.now() + self.SESSION_TTL,
        )
        self._sessions[token] = sess
        return sess

    def verify_session(self, token: str) -> Optional[Session]:
        sess = self._sessions.get(token)
        if not sess:
            return None
        if datetime.now() > sess.expires_at:
            self._sessions.pop(token, None)
            return None
        # 二次校验:账户是否仍可用、授权是否仍有效
        acct = self._accounts.get(sess.username)
        if not acct or acct.status != AccountStatus.ACTIVE:
            return None
        auth = self._auth_manager.get(acct.auth_id)
        if not auth or not auth.is_valid():
            self.disable(sess.username)
            return None
        return sess

    def logout(self, token: str) -> None:
        self._sessions.pop(token, None)
