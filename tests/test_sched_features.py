"""定时任务新特性单测:per-task 输出夹 / 会话 kind / 漏跑监控 / 聊天向导槽位。"""
from datetime import datetime, timedelta

import pytest

from client.webui import scheduler as sch
from client.webui import sessions as sess_mod
from client.webui import writer as writer_mod
from client.webui import sched_results
from client.webui import pipeline as pl


# ---------- #2 输出文件夹 ----------

def test_scheduled_task_has_output_folder():
    t = sch.ScheduledTask(id="x", username="u", name="n", question="q",
                          output_folder="/tmp/out")
    assert t.output_folder == "/tmp/out"
    assert "output_folder" in t.to_dict()


def test_register_output_root_allows_path(tmp_path):
    sub = tmp_path / "明文"
    sub.mkdir()
    target = sub / "a.xlsx"
    with pytest.raises(PermissionError):
        writer_mod.enforce_excel_path(target)          # 未登记 → 拒绝
    writer_mod.register_output_root(str(tmp_path))
    assert writer_mod.enforce_excel_path(target) == target.resolve()


def test_task_output_subdirs(tmp_path):
    cipher, plain = sched_results.task_output_subdirs(str(tmp_path / "任务A"))
    assert cipher.name == "密文" and plain.name == "明文"
    assert cipher.is_dir() and plain.is_dir()


def test_export_encrypted_run_to_folder_id_only(tmp_path):
    """只有身份列(无 num_enc)→ 仍能组装出密文夹里的 Excel。"""
    import pandas as pd
    idcsv = tmp_path / "ids.csv"
    pd.DataFrame({"姓名": ["甲", "乙"], "大区": ["华东", "华北"]}).to_csv(idcsv, index=False)
    manifest = [{"sheet_name": "结果", "id_csv": str(idcsv), "num_enc": "",
                 "col_order": ["姓名", "大区"], "numeric_cols": []}]
    out = str(tmp_path / "任务A")
    writer_mod.register_output_root(out)
    p = sched_results.export_encrypted_run_to_folder("run1", manifest, out, stem="任务A", run_at="2026-06-16T09:00:00")
    assert p is not None and p.exists()
    assert p.parent.name == "密文"


def test_decrypt_runs_to_folder_uses_output_folder(tmp_path):
    import pandas as pd
    idcsv = tmp_path / "ids.csv"
    pd.DataFrame({"姓名": ["甲"], "大区": ["华东"]}).to_csv(idcsv, index=False)
    manifest = [{"sheet_name": "结果", "id_csv": str(idcsv), "num_enc": "",
                 "col_order": ["姓名", "大区"], "numeric_cols": []}]
    out = str(tmp_path / "任务A")
    writer_mod.register_output_root(out)
    runs = [{"run_id": "r1", "run_at": "2026-06-16T09:00:00", "manifest": manifest}]
    out_dir, outcomes = sched_results.decrypt_runs_to_folder(runs, "任务A", output_folder=out)
    assert out_dir.name == "明文"
    assert outcomes and outcomes[0]["ok"]
    assert list(out_dir.glob("*.xlsx"))


# ---------- #3 会话 kind ----------

def test_session_kind_roundtrip():
    s = sess_mod.ChatSession(id="s1", title="t", username="u", kind="scheduled", task_id="tk1")
    d = s.to_dict()
    assert d["kind"] == "scheduled" and d["task_id"] == "tk1"
    s2 = sess_mod.ChatSession.from_dict(d)
    assert s2.kind == "scheduled" and s2.task_id == "tk1"


def test_session_create_kind(tmp_path):
    store = sess_mod.SessionStore(root=tmp_path)
    s = store.create(username="u", title="⏰ T", kind="scheduled", task_id="tk1")
    assert s.kind == "scheduled"
    # 默认普通
    n = store.create(username="u")
    assert n.kind == "normal"


def test_message_wizard_roundtrip():
    m = sess_mod.Message(id="m", role="assistant", wizard={"question": "q", "cron": "0 9 * * *"})
    assert sess_mod.Message.from_dict(m.to_dict()).wizard["cron"] == "0 9 * * *"


# ---------- #4 漏跑监控 ----------

def test_missed_store_dedup(tmp_path):
    store = sch.MissedRunStore(path=tmp_path / "missed.json")
    store.add_deduped(username="u", task_id="t1", task_name="n", question="q",
                      due_at="2026-06-16T09:00:00", reason="r", needs_data=True)
    # 同任务同窗口再加 → 去重不增
    store.add_deduped(username="u", task_id="t1", task_name="n", question="q",
                      due_at="2026-06-16T09:00:00", reason="r", needs_data=True)
    assert store.count_pending("u") == 1
    assert len(store.list_pending("u")) == 1
    # 不同窗口 → 计 2
    store.add_deduped(username="u", task_id="t1", task_name="n", question="q",
                      due_at="2026-06-17T09:00:00", reason="r", needs_data=True)
    assert store.count_pending("u") == 2


def test_missed_store_resolve(tmp_path):
    store = sch.MissedRunStore(path=tmp_path / "missed.json")
    m = store.add(username="u", task_id="t1", task_name="n", question="q",
                  due_at="d", reason="r")
    store.set_status(m.id, "resolved")
    assert store.count_pending("u") == 0


def test_scheduler_detects_missed_vs_ontime(tmp_path):
    store = sch.TaskStore(path=tmp_path / "tasks.json")
    t = store.create(username="u", name="n", question="q")
    fired, missed = [], []
    sc = sch.Scheduler(store, on_fire=lambda x: fired.append(x.id),
                       on_miss=lambda x, due: missed.append(x.id),
                       miss_grace_seconds=600)
    # 设 next_run 为很久以前(服务当时没运行)→ 漏跑(可能跨多个到点期,各记一条)
    store._tasks[t.id].next_run = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    sc._tick()
    assert len(missed) >= 1 and all(x == t.id for x in missed) and fired == []

    # 重置:next_run 在宽限内(刚到点)→ 正常触发
    fired.clear(); missed.clear()
    store._tasks[t.id].next_run = (datetime.now() - timedelta(seconds=5)).isoformat(timespec="seconds")
    sc._tick()
    assert fired == [t.id] and missed == []


# ---------- #1 聊天向导 ----------

def test_looks_like_schedule_request():
    assert pl.looks_like_schedule_request("每天早上9点算各大区回款率")
    assert pl.looks_like_schedule_request("帮我创建定时任务,每月1号汇总")
    assert pl.looks_like_schedule_request("每周一三五9点跑回款率")
    assert pl.looks_like_schedule_request("工作日上午统计完成率")
    assert not pl.looks_like_schedule_request("按大区统计销售完成率")
    assert not pl.looks_like_schedule_request("RFM 怎么算")
    # 含"每天"但不是建任务(普通分析)→ 不误触发
    assert not pl.looks_like_schedule_request("画每天的销售额趋势图")


def test_extract_task_slots(monkeypatch):
    def fake_llm(host, token, prompt, history=None, should_cancel=None, web_search=False):
        return '{"name":"每日回款率","question":"按大区统计本月回款率TOP10",' \
               '"schedule_text":"每天早上9点","needs_data":true}'
    monkeypatch.setattr(pl, "call_llm_for_freechat", fake_llm)
    slots = pl.extract_task_slots("h", "tok", "每天早上9点算回款率top10")
    assert slots["name"] == "每日回款率"
    assert slots["question"].startswith("按大区")
    assert slots["cron"] == "0 9 * * *"          # 自然语言 → cron
    assert slots["needs_data"] is True
    assert slots["missing"] == []                # question + schedule 都齐


def test_detect_intent_ambiguity():
    # 排程词 + 带附件 → 歧义,需澄清
    amb = pl.detect_intent_ambiguity("每天早上9点算各大区回款率,使用附件文档", has_attachment=True)
    assert amb and amb["kind"] == "schedule_vs_oneshot"
    assert {o["action"] for o in amb["options"]} == {"wizard", "analyze"}
    assert amb["allow_free"] is True
    # 排程词但没附件 → 不歧义(直接走向导)
    assert pl.detect_intent_ambiguity("每天早上9点算回款率", has_attachment=False) is None
    # 带附件但不是排程 → 不歧义(正常分析)
    assert pl.detect_intent_ambiguity("按大区统计回款率", has_attachment=True) is None


def test_extract_task_slots_llm_fail_fallback(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("llm down")
    monkeypatch.setattr(pl, "call_llm_for_freechat", boom)
    slots = pl.extract_task_slots("h", "tok", "每周一9点跑")
    # LLM 挂了仍兜底:question=原文,schedule 从原文解析
    assert slots["question"] == "每周一9点跑"
    assert slots["cron"] == "0 9 * * 1"
