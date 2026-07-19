"""
Admin 登录鉴权 + 账户(密码 / 绑定邮箱 / 邮箱验证码改密)。

- 初始账户:admin / 123456(首次运行自动建)。
- 登录后才能访问 /admin/*(中间件拦截),支持远程访问。
- 改密前必须先绑定邮箱;每次改密需要邮箱验证码(经 SMTP 发送)。
- 未配置 SMTP 时,验证码记录到服务端日志(便于本地自测),不在页面回显。

存储:~/.agent-system/admin/admin_auth.json(密码仅存 PBKDF2 盐+散列,不存明文)。
"""
from __future__ import annotations

import json
import secrets
import smtplib
import time
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Optional

from host.user_manager import _hash_password

STORE = Path.home() / ".agent-system" / "admin" / "admin_auth.json"
SESSION_TTL = 8 * 3600     # 登录态 8 小时
CODE_TTL = 600             # 验证码 10 分钟
COOKIE = "admin_session"

# 常见邮箱服务商预设:用户只需选服务商 + 填邮箱 + 授权码,服务器/端口/加密自动配好。
EMAIL_PRESETS = {
    "qq":      {"label": "QQ 邮箱(@qq.com)",        "host": "smtp.qq.com",        "port": 465, "ssl": True,
                "hint": "QQ 邮箱网页版 → 设置 → 账户 → 开启「IMAP/SMTP 服务」→ 按提示生成「授权码」(16 位字母,不是 QQ 登录密码),粘贴到下方。"},
    "163":     {"label": "163 邮箱(@163.com)",       "host": "smtp.163.com",       "port": 465, "ssl": True,
                "hint": "163 邮箱 → 设置 → POP3/SMTP/IMAP → 开启「SMTP 服务」→ 设置并复制「客户端授权密码」,粘贴到下方。"},
    "126":     {"label": "126 邮箱(@126.com)",       "host": "smtp.126.com",       "port": 465, "ssl": True,
                "hint": "126 邮箱 → 设置 → POP3/SMTP/IMAP → 开启 SMTP → 设置「客户端授权密码」,粘贴到下方。"},
    "gmail":   {"label": "Gmail(@gmail.com)",        "host": "smtp.gmail.com",     "port": 587, "tls": True,
                "hint": "需先开启两步验证,再到 Google 账户 → 安全 → 应用专用密码,生成 16 位密码,粘贴到下方。"},
    "outlook": {"label": "Outlook / Hotmail",         "host": "smtp.office365.com", "port": 587, "tls": True,
                "hint": "填 Outlook 邮箱与登录密码;若开了两步验证,请用「应用密码」。"},
    "custom":  {"label": "其他(手动填服务器)",        "host": "",                   "port": 587, "tls": True,
                "hint": "手动填写 SMTP 服务器地址、端口和加密方式(可向你的邮箱服务商客服索取)。"},
}


class AdminAuth:
    def __init__(self, path: Optional[Path] = None):
        self._path = path or STORE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._d = self._load()
        self._sessions: dict[str, float] = {}        # token -> 过期时间戳
        self._codes: dict[str, tuple[str, float]] = {}  # purpose -> (code, 过期)
        if not self._d:
            self._init_default()

    # ---- 持久化 ----
    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}

    def _save(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._d, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    def _init_default(self) -> None:
        salt = secrets.token_hex(16)
        self._d = {
            "username": "admin",
            "password_salt": salt,
            "password_hash": _hash_password("123456", salt),
            "email": "",
            "smtp": {},
        }
        self._save()

    # ---- 账户信息 ----
    @property
    def username(self) -> str:
        return self._d.get("username", "admin")

    @property
    def email(self) -> str:
        return self._d.get("email", "") or ""

    @property
    def smtp(self) -> dict:
        return self._d.get("smtp", {}) or {}

    @property
    def initialized(self) -> bool:
        """首次初始化(绑邮箱 + 邮件发送测试通过)是否完成。
        完成前:向导态,邮箱可自由改;完成后:改密码/邮箱都要验证码。"""
        return bool(self._d.get("initialized", False))

    def set_initialized(self, value: bool = True) -> None:
        self._d["initialized"] = bool(value)
        self._save()

    def is_default_password(self) -> bool:
        """是否仍在使用出厂默认口令 123456 —— 用于在 UI 上提示强制改密。"""
        salt = self._d.get("password_salt", "")
        return bool(salt) and self._d.get("password_hash") == _hash_password("123456", salt)

    def verify_login(self, username: str, password: str) -> bool:
        if (username or "").strip() != self.username:
            return False
        want = self._d.get("password_hash", "")
        got = _hash_password(password or "", self._d.get("password_salt", ""))
        return bool(want) and secrets.compare_digest(got, want)

    def set_password(self, new_pw: str) -> None:
        salt = secrets.token_hex(16)
        self._d["password_salt"] = salt
        self._d["password_hash"] = _hash_password(new_pw, salt)
        self._save()
        self._sessions.clear()   # 改密后所有登录态失效

    def set_email(self, email: str) -> None:
        self._d["email"] = (email or "").strip()
        self._save()

    def set_smtp(self, cfg: dict) -> None:
        self._d["smtp"] = cfg or {}
        self._save()

    # ---- 登录态(内存,进程重启需重新登录)----
    def _gc(self) -> None:
        now = time.time()
        for t in [t for t, e in self._sessions.items() if e < now]:
            self._sessions.pop(t, None)

    def login(self) -> str:
        self._gc()
        tok = secrets.token_urlsafe(24)
        self._sessions[tok] = time.time() + SESSION_TTL
        return tok

    def valid(self, token: Optional[str]) -> bool:
        if not token:
            return False
        exp = self._sessions.get(token)
        if not exp:
            return False
        if time.time() > exp:
            self._sessions.pop(token, None)
            return False
        return True

    def logout(self, token: Optional[str]) -> None:
        if token:
            self._sessions.pop(token, None)

    # ---- 验证码 ----
    def gen_code(self, purpose: str = "change_pw") -> str:
        code = f"{secrets.randbelow(1_000_000):06d}"
        self._codes[purpose] = (code, time.time() + CODE_TTL)
        return code

    def check_code(self, code: str, purpose: str = "change_pw") -> bool:
        rec = self._codes.get(purpose)
        if not rec:
            return False
        c, exp = rec
        if time.time() > exp:
            self._codes.pop(purpose, None)
            return False
        ok = secrets.compare_digest(c, (code or "").strip())
        if ok:
            self._codes.pop(purpose, None)   # 一次性
        return ok

    # ---- 发测试邮件(首次初始化第二步:验证 SMTP 是否真的能发)----
    def send_test_email(self, to: str) -> tuple[bool, str]:
        s = self.smtp
        if not (s.get("host") and s.get("from")):
            return False, "尚未填写邮件发送设置(服务商 / 发件邮箱 / 授权码)。"
        body = ("这是一封来自 Clawworker Admin 的配置测试邮件。\n\n"
                "收到它,说明邮件发送已配好;之后改密码 / 换邮箱的验证码会发到这里。")
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = "Clawworker Admin · 配置测试"
            msg["From"] = formataddr(("Clawworker Admin", s["from"]))
            msg["To"] = to
            port = int(s.get("port", 587))
            if s.get("use_ssl"):
                srv = smtplib.SMTP_SSL(s["host"], port, timeout=15)
            else:
                srv = smtplib.SMTP(s["host"], port, timeout=15)
                if s.get("use_tls", True):
                    srv.starttls()
            if s.get("user"):
                srv.login(s["user"], s.get("password", ""))
            srv.sendmail(s["from"], [to], msg.as_string())
            srv.quit()
            return True, f"测试邮件已发送到 {to},请查收"
        except Exception as e:  # noqa: BLE001
            return False, f"SMTP 发送失败({type(e).__name__}),请检查发件邮箱与授权码是否正确。"

    # ---- 发邮件(验证码)----
    def send_code_email(self, to: str, code: str, what: str = "登录密码") -> tuple[bool, str]:
        body = (f"你正在修改 Clawworker Admin 的{what}。\n\n验证码:{code}\n"
                f"10 分钟内有效。若非本人操作,请忽略本邮件并尽快检查账户安全。")
        s = self.smtp
        if not (s.get("host") and s.get("from")):
            print(f"[admin-auth] 未配置 SMTP · 改{what}验证码 → {to}: {code}", flush=True)
            return False, "尚未配置 SMTP,验证码已记录到服务端日志(本地自测用)。可在下方配置 SMTP 后真正发邮件。"
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["Subject"] = f"Clawworker Admin · 改{what}验证码"
            msg["From"] = formataddr(("Clawworker Admin", s["from"]))
            msg["To"] = to
            port = int(s.get("port", 587))
            if s.get("use_ssl"):
                srv = smtplib.SMTP_SSL(s["host"], port, timeout=15)
            else:
                srv = smtplib.SMTP(s["host"], port, timeout=15)
                if s.get("use_tls", True):
                    srv.starttls()
            if s.get("user"):
                srv.login(s["user"], s.get("password", ""))
            srv.sendmail(s["from"], [to], msg.as_string())
            srv.quit()
            return True, f"验证码已发送到 {to}"
        except Exception as e:  # noqa: BLE001
            print(f"[admin-auth] SMTP 发送失败: {e} · 验证码 → {to}: {code}", flush=True)
            return False, f"SMTP 发送失败({type(e).__name__});验证码已记录到服务端日志。请检查 SMTP 配置。"
