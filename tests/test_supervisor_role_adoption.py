"""supervisor 角色接管 —— 修「点管理端图标无反应」。

根因:supervisor 是单例,但桌面图标按角色分片启动(管理端只要 host、用户端只要 client)。
角色无关的单例锁会让先启动的角色**永久堵死**另一个:后来的 supervisor 撞锁直接退出,
而在跑的那个又不托管新角色 → host 永远起不来,用户看到"点了没反应"。
修法:撞锁前先把想要的角色并进「期望集合」,在跑的 supervisor 每轮读取并接管。
"""
from __future__ import annotations

import json

import pytest

from host import service_manager as sm


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    monkeypatch.setattr(sm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sm, "DESIRED_FILE", tmp_path / "desired_services.json")
    return tmp_path


def test_desired_starts_empty(tmp_state):
    assert sm.read_desired() == []


def test_add_desired_unions_roles(tmp_state):
    assert sm.add_desired(["client"]) == ["client"]
    # 管理端图标随后请求 host —— 必须并集,不能覆盖掉 client
    assert sm.add_desired(["host"]) == ["client", "host"]
    assert sorted(sm.read_desired()) == ["client", "host"]


def test_add_desired_is_idempotent(tmp_state):
    sm.add_desired(["host"])
    sm.add_desired(["host"])
    assert sm.read_desired() == ["host"]


def test_add_desired_ignores_unknown_roles(tmp_state):
    assert sm.add_desired(["host", "不存在的角色"]) == ["host"]


def test_read_desired_survives_corrupt_file(tmp_state):
    (tmp_state / "desired_services.json").write_text("{坏JSON", encoding="utf-8")
    assert sm.read_desired() == []      # 损坏不得让 supervisor 崩


def test_supervisor_merges_desired_on_startup_and_adopts_in_loop():
    """守门:单例分支必须写期望集合,主循环必须读它接管新角色。"""
    import inspect
    import supervisor as sup
    src = inspect.getsource(sup.main)
    handoff = inspect.getsource(sup._handoff_or_replace_existing)
    # 撞上兼容单例时要把本进程角色并进期望集合(否则该角色永远没人管)
    assert "add_desired" in handoff, "兼容单例退出前没有把角色并进期望集合"
    # 撞上旧协议时必须替换,不能把请求交给不认识新角色/端口的旧进程。
    assert "terminate_incompatible_supervisor" in handoff
    # 主循环要读期望集合并接管
    loop = src[src.index("while _running"):]
    assert "read_desired" in loop, "主循环没有接管新角色"


def test_compatible_supervisor_receives_role_request(monkeypatch):
    import supervisor as sup

    monkeypatch.setattr(sm, "supervisor_running", lambda require_compatible=False: (True, 1234))
    monkeypatch.setattr(sm, "supervisor_state_compatible", lambda: True)
    merged = []
    monkeypatch.setattr(sm, "add_desired", lambda want: merged.extend(want) or sorted(want))

    assert sup._handoff_or_replace_existing(["host"]) is True
    assert merged == ["host"]


def test_incompatible_supervisor_is_replaced(monkeypatch):
    import supervisor as sup

    stopped = []
    reset = []
    monkeypatch.setattr(sm, "supervisor_running", lambda require_compatible=False: (True, 9744))
    monkeypatch.setattr(sm, "supervisor_state_compatible", lambda: False)
    monkeypatch.setattr(
        sm,
        "terminate_incompatible_supervisor",
        lambda pid: stopped.append(pid) or True,
    )
    monkeypatch.setattr(sm, "set_desired", lambda want: reset.extend(want) or sorted(want))

    assert sup._handoff_or_replace_existing(["host"]) is False
    assert stopped == [9744]
    assert reset == ["host"]


def test_incompatible_supervisor_stop_failure_is_explicit(monkeypatch):
    import supervisor as sup

    monkeypatch.setattr(sm, "supervisor_running", lambda require_compatible=False: (True, 9744))
    monkeypatch.setattr(sm, "supervisor_state_compatible", lambda: False)
    monkeypatch.setattr(sm, "terminate_incompatible_supervisor", lambda pid: False)

    with pytest.raises(RuntimeError, match="无法终止旧 supervisor"):
        sup._handoff_or_replace_existing(["host"])
