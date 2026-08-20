"""Clawworker 运行目录集中定义。"""

from __future__ import annotations

import os
from pathlib import Path


def _resolved_env_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else fallback


APP_DATA_DIR = _resolved_env_path(
    "CLAWWORKER_DATA_DIR", Path.home() / ".agent-system",
)
DOWNLOADS_DIR = _resolved_env_path(
    "CLAWWORKER_DOWNLOADS_DIR", Path.home() / "Downloads",
)
HOST_AUTH_DIR = APP_DATA_DIR / "host-auth"
HOST_CONFIG_DIR = APP_DATA_DIR / "host-config"
HOST_DATA_DIR = APP_DATA_DIR / "host-data"
ADMIN_DIR = APP_DATA_DIR / "admin"
SUPERVISOR_DIR = APP_DATA_DIR / "supervisor"
SCHEDULER_DIR = APP_DATA_DIR / "scheduler"
SESSIONS_DIR = APP_DATA_DIR / "sessions"
OUTPUTS_DIR = APP_DATA_DIR / "outputs"
CIPHERTEXT_DIR = APP_DATA_DIR / "ciphertexts"
KEYSTORE_DIR = APP_DATA_DIR / "keystore"
AUDIT_DIR = APP_DATA_DIR / "audit"
HOST_TRUST_DIR = APP_DATA_DIR / "host-trust"
USER_SKILLS_DIR = APP_DATA_DIR / "user_skills"
