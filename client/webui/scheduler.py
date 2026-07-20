"""
定时任务(MVP)。

设计(用户决策):
  - token「仅活跃会话」:无密文任务到点且已登录 → 直接跑;否则不跑。
  - 密态分析「不自动解密」:到点 → 进**待批队列**,用户回来在收件箱点「运行」
    才真正执行(此时人在场,走正常解密授权卡)。
  - 通用:任务可带密文(分析)或不带(自由问答 / 文本任务)。

组成:
  ScheduledTask + TaskStore        —— 任务定义 + JSON 持久化
  PendingRun + PendingStore        —— 待批队列(有密文的到点入这)
  RunRecord + HistoryStore         —— 运行历史
  Scheduler                        —— 后台线程,tick 检查到点任务,调 on_fire 回调

持久化目录:~/.agent-system/scheduler/
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional


SCHED_DIR = Path.home() / ".agent-system" / "scheduler"

# 单次 tick 里为一个任务最多枚举多少个漏跑期(防长期停机 × 高频任务爆量;约一年每日)
_MAX_MISS_ENUM = 366


def _now() -> datetime:
    return datetime.now()


def _iso(dt: Optional[datetime]) -> str:
    return dt.isoformat(timespec="seconds") if dt else ""


# ---------------------------------------------------------------------------
# 自然语言 → cron 解析(普通用户用大白话,自动转 cron)
# ---------------------------------------------------------------------------

import re as _re

_WEEKDAY_CHAR = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 0, "天": 0}
_WEEKDAY_CN = {0: "日", 1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六"}


def _parse_clock(text: str) -> tuple[int, int]:
    """从文本抽时间,默认 09:00。支持 早上/上午/中午/下午/晚上 + N点[半|N分]。"""
    hour, minute = 9, 0
    m = _re.search(
        r"(凌晨|早上|早晨|上午|中午|下午|晚上|傍晚|夜里|夜晚)?\s*(\d{1,2})\s*[点时:：]\s*(半|(\d{1,2})\s*分?)?",
        text,
    )
    if m:
        period = m.group(1)
        hour = int(m.group(2))
        if m.group(3) == "半":
            minute = 30
        elif m.group(4):
            minute = int(m.group(4))
        if period in ("下午", "晚上", "傍晚", "夜里", "夜晚") and hour < 12:
            hour += 12
        elif period == "中午" and hour != 12:
            hour += 12
    return min(hour, 23), min(minute, 59)


def parse_natural_schedule(text: str) -> dict:
    """
    把中文大白话排程描述转成 cron。返回 {ok, cron, readable, error}。
    例:
      每月1号           → 0 9 1 * *
      每周一三五9点     → 0 9 * * 1,3,5
      工作日上午9点     → 0 9 * * 1-5
      每天晚上8点       → 0 20 * * *
      每2小时           → 0 */2 * * *
      每15分钟          → */15 * * * *
    """
    t = (text or "").strip()
    if not t:
        return {"ok": False, "error": "请输入描述,如「每月1号」"}
    hour, minute = _parse_clock(t)
    hm = f"{hour:02d}:{minute:02d}"

    # 每 N 分钟
    m = _re.search(r"每\s*隔?\s*(\d+)\s*分钟", t)
    if m:
        n = int(m.group(1))
        return {"ok": True, "cron": f"*/{n} * * * *", "readable": f"每 {n} 分钟"}
    # 每 N 小时
    m = _re.search(r"每\s*隔?\s*(\d+)\s*个?\s*小时", t)
    if m:
        n = int(m.group(1))
        return {"ok": True, "cron": f"0 */{n} * * *", "readable": f"每 {n} 小时"}
    if _re.search(r"每\s*个?\s*小时", t):
        return {"ok": True, "cron": "0 * * * *", "readable": "每小时"}

    # 每月 N 号(可多个)
    if "每月" in t or "每个月" in t or _re.search(r"\d{1,2}\s*[号日]", t):
        days = _re.findall(r"(\d{1,2})\s*[号日]", t)
        days = [d for d in days if 1 <= int(d) <= 31]
        if days and ("月" in t or "每月" in t or "每个月" in t):
            dstr = ",".join(sorted(set(days), key=int))
            return {"ok": True, "cron": f"{minute} {hour} {dstr} * *",
                    "readable": f"每月 {dstr} 号 {hm}"}

    # 工作日 / 周末
    if "工作日" in t:
        return {"ok": True, "cron": f"{minute} {hour} * * 1-5", "readable": f"工作日 {hm}"}
    if "周末" in t or "双休" in t:
        return {"ok": True, "cron": f"{minute} {hour} * * 0,6", "readable": f"周末 {hm}"}

    # 每周 周X(支持 周一三五 / 周一、周三、周五 / 星期X)
    if "每周" in t or "每星期" in t or _re.search(r"(?:周|星期)[一二三四五六日天]", t):
        chars: list[str] = []
        for mm in _re.finditer(r"(?:周|星期)([一二三四五六日天]+)", t):
            chars.extend(list(mm.group(1)))
        nums = sorted(set(_WEEKDAY_CHAR[c] for c in chars if c in _WEEKDAY_CHAR))
        if nums:
            dstr = ",".join(str(n) for n in nums)
            cn = "、".join(_WEEKDAY_CN[n] for n in nums)
            return {"ok": True, "cron": f"{minute} {hour} * * {dstr}",
                    "readable": f"每周{cn} {hm}"}

    # 每天 / 每日 / 仅给了时间
    if "每天" in t or "每日" in t or _re.search(r"\d{1,2}\s*[点时]", t):
        return {"ok": True, "cron": f"{minute} {hour} * * *", "readable": f"每天 {hm}"}

    return {"ok": False,
            "error": "没听懂 · 试试:每月1号 / 每周一三五9点 / 每天晚上8点 / 工作日上午9点 / 每2小时"}


# ---------------------------------------------------------------------------
# 任务定义
# ---------------------------------------------------------------------------

@dataclass
class ScheduledTask:
    id: str
    username: str
    name: str
    question: str
    cipher_path: str = ""               # 固定密文(空 = 不绑固定文件)
    source_folder: str = ""             # 绑定源文件夹:到点取最新明文文件自动加密再分析
    source_pattern: str = ""            # 可选 glob(默认 *.csv/*.xlsx/*.xls 取最新)
    output_folder: str = ""             # 每任务专属输出文件夹(内自动分 密文/ 明文/);空=回退 ~/Downloads/<name>/
    web_search: bool = False            # 联网搜索(查天气/新闻/资料等实时信息;主要用于无数据的问答任务)
    # 周期:kind ∈ interval / daily / weekly / monthly / cron
    schedule_kind: str = "daily"
    interval_minutes: int = 60          # kind=interval 时用
    at_hour: int = 9                    # kind=daily/weekly/monthly 时用(0-23)
    at_minute: int = 0
    weekday: int = 0                    # kind=weekly 时用(0=周一 ... 6=周日)
    day_of_month: int = 1               # kind=monthly 时用(1-28)
    cron_expr: str = ""                 # kind=cron 时用(标准 5 段:分 时 日 月 周)
    cron_readable: str = ""             # 自定义排程的中文可读描述(给列表展示)
    enabled: bool = True
    session_id: str = ""                # 该任务的聊天会话(累积它的历次运行)
    last_fired: str = ""
    next_run: str = ""
    fail_streak: int = 0                # 连续失败次数(成功清零);达阈值自动暂停 + 告警
    auto_paused_reason: str = ""        # 因连续失败被自动暂停的原因(非空 = 熔断暂停)
    created_at: str = field(default_factory=lambda: _iso(_now()))

    @property
    def needs_approval(self) -> bool:
        # 有数据(固定密文 或 源文件夹)→ 密态分析,结果加密暂存待批量解密
        return bool(self.cipher_path or self.source_folder)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["needs_approval"] = self.needs_approval
        return d

    def compute_next_run(self, after: Optional[datetime] = None) -> datetime:
        base = after or _now()
        if self.schedule_kind == "cron" and self.cron_expr.strip():
            try:
                from croniter import croniter
                return croniter(self.cron_expr.strip(), base).get_next(datetime)
            except Exception:
                # cron 解析失败 → 退化为每天兜底
                pass
        if self.schedule_kind == "interval":
            mins = max(1, int(self.interval_minutes or 60))
            return base + timedelta(minutes=mins)
        if self.schedule_kind == "weekly":
            target = base.replace(hour=self.at_hour, minute=self.at_minute,
                                  second=0, microsecond=0)
            days_ahead = (self.weekday - base.weekday()) % 7
            target = target + timedelta(days=days_ahead)
            if target <= base:
                target = target + timedelta(days=7)
            return target
        if self.schedule_kind == "monthly":
            # 想要的日子超过当月天数(31 号遇到 2 月)→ 落到该月**最后一天**,即"月末"语义。
            # 旧实现一律夹到 28 号:用户设「每月31号」跑月结,1月/3月(31天)也在28号跑,
            # 提前 3 天赶在月末结账前,而 UI 仍显示「每月 31 号」—— 显示与实际不符。
            import calendar
            want = min(max(int(self.day_of_month or 1), 1), 31)

            def _at(y: int, m: int) -> datetime:
                d = min(want, calendar.monthrange(y, m)[1])
                return datetime(y, m, d, self.at_hour, self.at_minute)

            target = _at(base.year, base.month)
            if target <= base:
                y, m = (base.year + 1, 1) if base.month == 12 else (base.year, base.month + 1)
                target = _at(y, m)
            return target
        # daily(默认)
        target = base.replace(hour=self.at_hour, minute=self.at_minute,
                              second=0, microsecond=0)
        if target <= base:
            target = target + timedelta(days=1)
        return target


@dataclass
class PendingRun:
    id: str
    username: str
    task_id: str
    task_name: str
    question: str
    cipher_path: str
    session_id: str
    due_at: str
    status: str = "pending"             # pending / approved / dismissed
    created_at: str = field(default_factory=lambda: _iso(_now()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EncryptedResult:
    """定时密态任务一次运行的加密结果(暂存沙盒,待批量解密)。"""
    id: str
    username: str
    task_id: str
    task_name: str
    run_id: str                          # sched_results 沙盒目录名
    run_at: str
    question: str = ""
    manifest: list = field(default_factory=list)   # sched_results.persist 的 manifest
    status: str = "pending"              # pending / decrypted

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RunRecord:
    id: str
    username: str
    task_id: str
    task_name: str
    ran_at: str
    status: str                         # done / failed / launched
    summary: str = ""
    excel_name: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 持久化 store(轻量 JSON)
# ---------------------------------------------------------------------------

class _JsonStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _write(self, items: list[dict]) -> None:
        try:
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except Exception:
            pass


class TaskStore(_JsonStore):
    def __init__(self, path: Optional[Path] = None):
        super().__init__(path or SCHED_DIR / "tasks.json")
        self._tasks: dict[str, ScheduledTask] = {}
        for d in self._read():
            try:
                t = ScheduledTask(**{k: d.get(k) for k in ScheduledTask.__dataclass_fields__ if k in d})
                self._tasks[t.id] = t
            except Exception:
                continue

    def _flush(self):
        self._write([t.to_dict() for t in self._tasks.values()])

    def list_for(self, username: str) -> list[ScheduledTask]:
        out = [t for t in self._tasks.values() if t.username == username]
        out.sort(key=lambda t: t.created_at)
        return out

    def all_enabled(self) -> list[ScheduledTask]:
        return [t for t in self._tasks.values() if t.enabled]

    def get(self, tid: str) -> Optional[ScheduledTask]:
        return self._tasks.get(tid)

    def create(self, **kw) -> ScheduledTask:
        with self._lock:
            tid = secrets.token_hex(5)
            while tid in self._tasks:
                tid = secrets.token_hex(5)
            t = ScheduledTask(id=tid, **kw)
            t.next_run = _iso(t.compute_next_run())
            self._tasks[tid] = t
            self._flush()
            return t

    def update(self, tid: str, **patch) -> Optional[ScheduledTask]:
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return None
            for k, v in patch.items():
                if hasattr(t, k):
                    setattr(t, k, v)
            t.next_run = _iso(t.compute_next_run())
            self._flush()
            return t

    def mark_fired(self, tid: str) -> None:
        with self._lock:
            t = self._tasks.get(tid)
            if not t:
                return
            now = _now()
            t.last_fired = _iso(now)
            t.next_run = _iso(t.compute_next_run(now))
            self._flush()

    def delete(self, tid: str) -> bool:
        with self._lock:
            if tid not in self._tasks:
                return False
            self._tasks.pop(tid)
            self._flush()
            return True


class PendingStore(_JsonStore):
    def __init__(self, path: Optional[Path] = None):
        super().__init__(path or SCHED_DIR / "pending.json")
        self._items: dict[str, PendingRun] = {}
        for d in self._read():
            try:
                p = PendingRun(**{k: d.get(k) for k in PendingRun.__dataclass_fields__ if k in d})
                self._items[p.id] = p
            except Exception:
                continue

    def _flush(self):
        self._write([p.to_dict() for p in self._items.values()])

    def add(self, **kw) -> PendingRun:
        with self._lock:
            pid = secrets.token_hex(5)
            while pid in self._items:
                pid = secrets.token_hex(5)
            p = PendingRun(id=pid, **kw)
            self._items[pid] = p
            self._flush()
            return p

    def list_pending(self, username: str) -> list[PendingRun]:
        out = [p for p in self._items.values()
               if p.username == username and p.status == "pending"]
        out.sort(key=lambda p: p.created_at, reverse=True)
        return out

    def get(self, pid: str) -> Optional[PendingRun]:
        return self._items.get(pid)

    def set_status(self, pid: str, status: str) -> None:
        with self._lock:
            p = self._items.get(pid)
            if p:
                p.status = status
                self._flush()

    def count_pending(self, username: str) -> int:
        return sum(1 for p in self._items.values()
                   if p.username == username and p.status == "pending")

    def delete_for_task(self, task_id: str) -> int:
        with self._lock:
            ids = [i for i, p in self._items.items() if p.task_id == task_id]
            for i in ids:
                self._items.pop(i, None)
            if ids:
                self._flush()
            return len(ids)


class HistoryStore(_JsonStore):
    def __init__(self, path: Optional[Path] = None):
        super().__init__(path or SCHED_DIR / "history.json")
        self._items: list[RunRecord] = []
        for d in self._read():
            try:
                self._items.append(RunRecord(**{k: d.get(k) for k in RunRecord.__dataclass_fields__ if k in d}))
            except Exception:
                continue

    def add(self, **kw) -> RunRecord:
        with self._lock:
            rid = secrets.token_hex(5)
            r = RunRecord(id=rid, **kw)
            self._items.append(r)
            # 只留最近 200 条
            self._items = self._items[-200:]
            self._write([x.to_dict() for x in self._items])
            return r

    def list_for(self, username: str, limit: int = 50) -> list[RunRecord]:
        out = [r for r in self._items if r.username == username]
        out.sort(key=lambda r: r.ran_at, reverse=True)
        return out[:limit]

    def delete_for_task(self, task_id: str) -> int:
        with self._lock:
            before = len(self._items)
            self._items = [r for r in self._items if r.task_id != task_id]
            removed = before - len(self._items)
            if removed:
                self._write([x.to_dict() for x in self._items])
            return removed


class EncryptedResultStore(_JsonStore):
    """定时密态任务的加密结果累积(按任务聚合,批量解密)。"""

    def __init__(self, path: Optional[Path] = None):
        super().__init__(path or SCHED_DIR / "enc_results.json")
        self._items: dict[str, EncryptedResult] = {}
        for d in self._read():
            try:
                r = EncryptedResult(**{k: d.get(k) for k in EncryptedResult.__dataclass_fields__ if k in d})
                self._items[r.id] = r
            except Exception:
                continue

    def _flush(self):
        self._write([r.to_dict() for r in self._items.values()])

    def add(self, **kw) -> EncryptedResult:
        with self._lock:
            rid = secrets.token_hex(5)
            while rid in self._items:
                rid = secrets.token_hex(5)
            r = EncryptedResult(id=rid, **kw)
            self._items[rid] = r
            self._flush()
            return r

    def pending_for_task(self, task_id: str) -> list[EncryptedResult]:
        out = [r for r in self._items.values() if r.task_id == task_id and r.status == "pending"]
        out.sort(key=lambda r: r.run_at)
        return out

    def aggregate_by_task(self, username: str) -> list[dict[str, Any]]:
        """按任务聚合待批的加密结果 —— 1 任务 1 条(无论跑了几次)。"""
        agg: dict[str, dict[str, Any]] = {}
        for r in self._items.values():
            if r.username != username or r.status != "pending":
                continue
            a = agg.setdefault(r.task_id, {
                "task_id": r.task_id, "task_name": r.task_name,
                "question": r.question, "count": 0, "latest_run": "",
            })
            a["count"] += 1
            if r.run_at > a["latest_run"]:
                a["latest_run"] = r.run_at
        out = list(agg.values())
        out.sort(key=lambda x: x["latest_run"], reverse=True)
        return out

    def count_pending(self, username: str) -> int:
        return len({r.task_id for r in self._items.values()
                    if r.username == username and r.status == "pending"})

    def mark_decrypted(self, ids: list[str]) -> None:
        with self._lock:
            for i in ids:
                if i in self._items:
                    self._items[i].status = "decrypted"
            self._flush()

    def delete_for_task(self, task_id: str) -> int:
        with self._lock:
            ids = [i for i, r in self._items.items() if r.task_id == task_id]
            for i in ids:
                self._items.pop(i, None)
            if ids:
                self._flush()
            return len(ids)


@dataclass
class MissedRun:
    """一次本该执行却没跑成的运行(漏跑)—— 服务当时没运行 / 未登录 / 无数据。
    供监控预警 + 手动补救(补救时让用户指定该轮数据文件)。"""
    id: str
    username: str
    task_id: str
    task_name: str
    question: str
    due_at: str                         # 本应执行的时间
    reason: str                         # 漏跑原因(中文)
    needs_data: bool = False            # 该任务需不需要数据(补救时是否要选文件)
    status: str = "pending"             # pending / resolved / dismissed
    created_at: str = field(default_factory=lambda: _iso(_now()))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class MissedRunStore(_JsonStore):
    def __init__(self, path: Optional[Path] = None):
        super().__init__(path or SCHED_DIR / "missed.json")
        self._items: dict[str, MissedRun] = {}
        for d in self._read():
            try:
                m = MissedRun(**{k: d.get(k) for k in MissedRun.__dataclass_fields__ if k in d})
                self._items[m.id] = m
            except Exception:
                continue

    def _flush(self):
        self._write([m.to_dict() for m in self._items.values()])

    def add(self, **kw) -> "MissedRun":
        with self._lock:
            mid = secrets.token_hex(5)
            while mid in self._items:
                mid = secrets.token_hex(5)
            m = MissedRun(id=mid, **kw)
            self._items[mid] = m
            self._flush()
            return m

    def has_pending(self, task_id: str, due_at: str) -> bool:
        """去重:同一任务同一到点窗口只记一条 pending。"""
        return any(m.task_id == task_id and m.due_at == due_at and m.status == "pending"
                   for m in self._items.values())

    def add_deduped(self, **kw) -> Optional["MissedRun"]:
        if self.has_pending(kw.get("task_id", ""), kw.get("due_at", "")):
            return None
        return self.add(**kw)

    def list_pending(self, username: str) -> list["MissedRun"]:
        out = [m for m in self._items.values()
               if m.username == username and m.status == "pending"]
        out.sort(key=lambda m: m.due_at, reverse=True)
        return out

    def get(self, mid: str) -> Optional["MissedRun"]:
        return self._items.get(mid)

    def set_status(self, mid: str, status: str) -> None:
        with self._lock:
            m = self._items.get(mid)
            if m:
                m.status = status
                self._flush()

    def count_pending(self, username: str) -> int:
        return sum(1 for m in self._items.values()
                   if m.username == username and m.status == "pending")

    def delete_for_task(self, task_id: str) -> int:
        with self._lock:
            ids = [i for i, m in self._items.items() if m.task_id == task_id]
            for i in ids:
                self._items.pop(i, None)
            if ids:
                self._flush()
            return len(ids)


# ---------------------------------------------------------------------------
# 调度器
# ---------------------------------------------------------------------------

class Scheduler:
    """后台线程 · 每 poll 秒检查到点任务 · 调 on_fire(task)。"""

    def __init__(self, task_store: TaskStore, on_fire: Callable[[ScheduledTask], None],
                 poll_seconds: int = 30,
                 on_miss: Optional[Callable[[ScheduledTask, datetime], None]] = None,
                 miss_grace_seconds: int = 600):
        self._tasks = task_store
        self._on_fire = on_fire
        self._on_miss = on_miss
        self._miss_grace = miss_grace_seconds   # next_run 过期超过这个秒数 = 漏跑(服务当时没运行)
        self._poll = poll_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception:
                pass
            self._stop.wait(self._poll)

    def _tick(self) -> None:
        now = _now()
        for t in self._tasks.all_enabled():
            if not t.next_run:
                self._tasks.update(t.id, enabled=True)  # 触发 next_run 计算
                continue
            try:
                nxt = datetime.fromisoformat(t.next_run)
            except Exception:
                self._tasks.mark_fired(t.id)
                continue
            if nxt <= now:
                # 枚举 [nxt, now] 之间**所有**到点时刻 —— 停机多天的每日任务会漏 N 期,
                # 不能像原来那样只记 1 条(mark_fired 直接跳到 now 之后会吞掉中间各期)。
                # 期数可能极多(如 */5 停机数天),用滑动窗口只保留**最近** _MAX_MISS_ENUM 期:
                # 用户最关心的是离现在最近的漏跑,且必须拿到真正的最新一期来判断是否"现在该跑"。
                occ = deque(maxlen=_MAX_MISS_ENUM)   # 只保留**最近** N 期(超长停机丢弃更旧的)
                m = nxt
                while m <= now:
                    occ.append(m)
                    m = t.compute_next_run(m)
                occ = list(occ)
                # 先推进 next_run 到 now 之后,避免回调异常导致重复触发
                self._tasks.mark_fired(t.id)
                last = occ[-1]              # 真正最新一期(occ 至少含 nxt,不会空)
                # 最近一期若在宽限内 = 服务刚回来、这期算"到点该跑"→ 触发它;更早的都是漏跑
                fire_last = (now - last).total_seconds() <= self._miss_grace
                missed = occ[:-1] if fire_last else occ
                for md in missed:
                    if self._on_miss is not None:
                        try:
                            self._on_miss(t, md)
                        except Exception:
                            pass
                if fire_last:
                    try:
                        self._on_fire(t)
                    except Exception:
                        pass
