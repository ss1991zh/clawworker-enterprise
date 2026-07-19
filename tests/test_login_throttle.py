"""登录限速回归测试。"""
from __future__ import annotations

from host.login_throttle import LoginThrottle, _FAIL_THRESHOLD


def test_no_lock_before_threshold():
    t = LoginThrottle()
    for _ in range(_FAIL_THRESHOLD - 1):
        t.record_failure("u:alice")
    assert t.check("u:alice") == 0.0   # 未达阈值不锁


def test_lock_after_threshold():
    t = LoginThrottle()
    for _ in range(_FAIL_THRESHOLD):
        t.record_failure("u:bob")
    assert t.check("u:bob") > 0.0      # 达阈值即锁


def test_success_resets():
    t = LoginThrottle()
    for _ in range(_FAIL_THRESHOLD):
        t.record_failure("ip:1.2.3.4")
    assert t.check("ip:1.2.3.4") > 0.0
    t.record_success("ip:1.2.3.4")
    assert t.check("ip:1.2.3.4") == 0.0   # 成功清零


def test_keys_are_independent():
    t = LoginThrottle()
    for _ in range(_FAIL_THRESHOLD):
        t.record_failure("u:carol")
    assert t.check("u:carol") > 0.0
    assert t.check("u:dave") == 0.0       # 互不影响


def test_default_password_detection():
    import tempfile
    from pathlib import Path
    from host.admin_auth import AdminAuth
    d = Path(tempfile.mkdtemp())
    a = AdminAuth(d / "admin_auth.json")
    assert a.is_default_password() is True     # 出厂默认 123456
    a.set_password("a-strong-new-secret")
    assert a.is_default_password() is False
