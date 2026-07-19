"""
pytest fixtures —— v4 简化版,只剩 zfhe + 临时 Downloads + 授权器。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from client.permissions import AutoApproveAuthorizer
from client.tools import ZFHE


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_backend: 需真实 HE 后端 + 本机密钥 vault(缺则自动 skip)",
    )


@pytest.fixture
def zfhe() -> ZFHE:
    return ZFHE()


@pytest.fixture
def authorizer():
    return AutoApproveAuthorizer()


# ---------------------------------------------------------------------------
# 临时 Downloads 目录(测试隔离)
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_downloads(tmp_path: Path, monkeypatch) -> Path:
    """把 ~/Downloads/ 重定向到临时目录,避免污染真实用户目录。"""
    fake = tmp_path / "Downloads"
    fake.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("client.permissions.DOWNLOADS_DIR", fake)
    return fake


# ---------------------------------------------------------------------------
# 样例数据(明文,只用于测试)
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_sales_rows() -> list[dict]:
    return [
        {"month": "2024-01", "amount": 100, "category": "A"},
        {"month": "2024-01", "amount": 200, "category": "B"},
        {"month": "2024-02", "amount": 150, "category": "A"},
        {"month": "2024-02", "amount": 250, "category": "B"},
        {"month": "2024-03", "amount": 300, "category": "A"},
    ]
