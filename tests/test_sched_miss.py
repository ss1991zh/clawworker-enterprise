"""定时任务漏跑多期枚举测试(P2-5):停机多天不再只记 1 条。"""
from __future__ import annotations

from datetime import timedelta

from client.webui import scheduler as sch


def _make_store(tmp_path, next_run_iso):
    store = sch.TaskStore(path=tmp_path / "tasks.json")
    t = store.create(username="u", name="日报", question="出日报",
                     schedule_kind="daily", at_hour=9, at_minute=0, enabled=True)
    t.next_run = next_run_iso          # 直接改实例(create/update 会重算 next_run)
    store._flush()
    return store, t.id


def test_multi_period_miss_enumerated(tmp_path):
    now = sch._now()
    # 每日任务的 next_run 在 5 天前 → 应记 ~5 条漏跑,而非 1 条
    five_days_ago = (now - timedelta(days=5)).replace(hour=9, minute=0, second=0, microsecond=0)
    store, tid = _make_store(tmp_path, sch._iso(five_days_ago))

    misses = []
    fires = []
    scd = sch.Scheduler(store, on_fire=lambda t: fires.append(t),
                        on_miss=lambda t, due: misses.append(due), miss_grace_seconds=600)
    scd._tick()

    assert len(misses) >= 4, f"应枚举出多期漏跑,实际 {len(misses)}"
    # 各期 due 互不相同(按天)
    assert len({m.date() for m in misses}) == len(misses)
    # next_run 已推进到未来
    t = store.get(tid)
    assert sch.datetime.fromisoformat(t.next_run) > now


def test_no_miss_when_up_to_date(tmp_path):
    now = sch._now()
    future = (now + timedelta(hours=2))
    store, tid = _make_store(tmp_path, sch._iso(future))
    misses, fires = [], []
    scd = sch.Scheduler(store, on_fire=lambda t: fires.append(t),
                        on_miss=lambda t, due: misses.append(due))
    scd._tick()
    assert not misses and not fires    # 还没到点,不漏跑不触发
