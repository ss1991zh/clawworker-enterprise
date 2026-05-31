"""
主机侧:user_authorization + 用户管理 + LLM 文本解析。

⚠️ 术语订正(v3):"证书"在实际实现中就是 user_authorization 文件。
   旧的 JSON 信封测试已移除;过期路径验证留给客户端 SDK init 失败上报场景。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from host.cert_manager import AuthorizationManager
from host.llm_proxy import parse_llm_text
from host.user_manager import AccountStatus, UserManager


# ---------------------------------------------------------------------------
# 准备一个临时的 user_authorization 文件
# ---------------------------------------------------------------------------


def _write_authorization_file(path: Path, content: bytes = b"FAKE_AUTHORIZATION_BLOB") -> Path:
    """模拟外部平台签发的 user_authorization 文件。"""
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# AuthorizationManager
# ---------------------------------------------------------------------------


def test_import_authorization(tmp_path: Path):
    am = AuthorizationManager(storage_root=tmp_path / "host-auth")
    src = _write_authorization_file(tmp_path / "alice.auth")
    auth = am.import_authorization(username="alice", source=src)

    assert auth.subject == "alice"
    assert auth.is_valid()
    assert auth.file_path.exists()
    # 不同 fingerprint
    assert len(auth.auth_id) == 12


def test_import_authorization_missing_source(tmp_path: Path):
    am = AuthorizationManager(storage_root=tmp_path / "host-auth")
    with pytest.raises(FileNotFoundError):
        am.import_authorization(username="alice", source=tmp_path / "nonexistent")


def test_revoke_authorization(tmp_path: Path):
    am = AuthorizationManager(storage_root=tmp_path / "host-auth")
    src = _write_authorization_file(tmp_path / "alice.auth")
    am.import_authorization(username="alice", source=src)
    am.revoke("alice")
    assert not am.get_by_username("alice").is_valid()


def test_report_init_failed(tmp_path: Path):
    """客户端 hp.initDict 失败 → 上报 → 主机标记授权失效。"""
    am = AuthorizationManager(storage_root=tmp_path / "host-auth")
    src = _write_authorization_file(tmp_path / "alice.auth")
    am.import_authorization(username="alice", source=src)
    assert am.get_by_username("alice").is_valid()

    am.report_init_failed("alice")
    assert not am.get_by_username("alice").is_valid()


# ---------------------------------------------------------------------------
# 账户 + 登录
# ---------------------------------------------------------------------------


def test_account_requires_valid_authorization(tmp_path: Path):
    am = AuthorizationManager(storage_root=tmp_path / "host-auth")
    src = _write_authorization_file(tmp_path / "alice.auth")
    am.import_authorization(username="alice", source=src)
    am.revoke("alice")  # 立即失效

    um = UserManager(am)
    with pytest.raises(ValueError, match="user_authorization"):
        um.create_account(username="alice", password="pw")


def test_login_success_and_session(tmp_path: Path):
    am = AuthorizationManager(storage_root=tmp_path / "host-auth")
    src = _write_authorization_file(tmp_path / "alice.auth")
    am.import_authorization(username="alice", source=src)
    um = UserManager(am)
    um.create_account(username="alice", password="pw")

    sess = um.login(username="alice", password="pw")
    assert sess.username == "alice"
    assert um.verify_session(sess.token).username == "alice"


def test_login_wrong_password(tmp_path: Path):
    am = AuthorizationManager(storage_root=tmp_path / "host-auth")
    src = _write_authorization_file(tmp_path / "alice.auth")
    am.import_authorization(username="alice", source=src)
    um = UserManager(am)
    um.create_account(username="alice", password="pw")

    with pytest.raises(PermissionError, match="密码"):
        um.login(username="alice", password="wrong")


def test_login_disabled_after_authorization_revoked(tmp_path: Path):
    """user_authorization 被吊销 → 登录失败 + 账户自动 disabled。"""
    am = AuthorizationManager(storage_root=tmp_path / "host-auth")
    src = _write_authorization_file(tmp_path / "alice.auth")
    am.import_authorization(username="alice", source=src)
    um = UserManager(am)
    um.create_account(username="alice", password="pw")

    am.revoke("alice")
    with pytest.raises(PermissionError, match="user_authorization"):
        um.login(username="alice", password="pw")

    # 自动被禁用
    assert um._accounts["alice"].status == AccountStatus.DISABLED


# ---------------------------------------------------------------------------
# LLM 文本解析
# ---------------------------------------------------------------------------


def test_parse_llm_text_valid():
    text = """
<computation_plan>
{
  "scenario": 1,
  "tool": "pandaseal",
  "ops": [{"op":"group_by","field":"month"}, {"op":"sum","field":"amount"}],
  "output": {"file": "~/Downloads/x.xlsx", "sheets": [{"name": "Monthly"}]}
}
</computation_plan>

<summary>
已按月份聚合销售额并生成折线图,详见 Excel。
</summary>
"""
    resp = parse_llm_text(text)
    assert resp.computation_plan.scenario == 1
    assert resp.computation_plan.tool == "pandaseal"
    assert "Excel" in resp.summary


def test_parse_llm_text_missing_block():
    text = "<summary>没有 plan 块</summary>"
    with pytest.raises(ValueError, match="computation_plan|JSON"):
        parse_llm_text(text)


def test_parse_llm_text_chinese_markers_fallback():
    """容错:LLM 用中文【】+ json fence,不用 <tag>。"""
    text = """
【1. computation_plan】
```json
{
  "scenario": 1,
  "tool": "pandaseal",
  "ops": [{"op":"mean"}],
  "output": {"file": "~/Downloads/x.xlsx", "sheets": [{"name":"S"}]}
}
```

【2. summary】
已生成均值结果。
"""
    resp = parse_llm_text(text)
    assert resp.computation_plan.scenario == 1
    assert "均值" in resp.summary
