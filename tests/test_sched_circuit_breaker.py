"""定时任务连续失败熔断 + 告警测试(R2-P0-3)。"""
from __future__ import annotations

import importlib

from client.webui import scheduler as sch
from client.webui.notices import NoticeStore

app_mod = importlib.import_module("client.webui.app")


def _setup(tmp_path, monkeypatch):
    ts = sch.TaskStore(path=tmp_path / "tasks.json")
    ns = NoticeStore(path=tmp_path / "notices.json")
    monkeypatch.setattr(app_mod, "_task_store", ts)
    monkeypatch.setattr(app_mod, "_notice_store", ns)
    t = ts.create(username="u", name="日报", question="q")
    return ts, ns, t


def test_failures_accumulate_and_auto_pause(tmp_path, monkeypatch):
    ts, ns, t = _setup(tmp_path, monkeypatch)
    # 前两次失败:累加、告警、仍启用
    app_mod._record_sched_outcome(t, ok=False, err="boom1")
    app_mod._record_sched_outcome(t, ok=False, err="boom2")
    cur = ts.get(t.id)
    assert cur.fail_streak == 2 and cur.enabled is True
    # 第三次:自动暂停
    app_mod._record_sched_outcome(t, ok=False, err="boom3")
    cur = ts.get(t.id)
    assert cur.fail_streak == 3 and cur.enabled is False
    assert "自动暂停" in cur.auto_paused_reason
    # 有告警通知产出
    assert ns.unread_count("u") >= 1


def test_success_resets_streak(tmp_path, monkeypatch):
    ts, ns, t = _setup(tmp_path, monkeypatch)
    app_mod._record_sched_outcome(t, ok=False, err="x")
    app_mod._record_sched_outcome(t, ok=False, err="x")
    assert ts.get(t.id).fail_streak == 2
    app_mod._record_sched_outcome(t, ok=True)
    assert ts.get(t.id).fail_streak == 0


def test_interactive_run_no_effect(tmp_path, monkeypatch):
    _setup(tmp_path, monkeypatch)
    app_mod._record_sched_outcome(None, ok=False, err="x")   # 交互式(无 sched_task)不报错
