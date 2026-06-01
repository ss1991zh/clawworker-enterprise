"""
后台任务管理。

每次 /ask 提交 → 创建一个 Job → 在工作线程跑 LangGraph workflow
→ 状态机:pending → running → done / failed。
所有 job 元数据持久化到 ~/.agent-system/history/{job_id}.json,主进程重启后仍能查看。
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import traceback
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

HISTORY_DIR = Path.home() / ".agent-system" / "history"


@dataclass
class Job:
    id: str
    username: str
    query: str
    ciphertext: str           # 密文文件路径
    schema_summary: str       # schema 摘要(给列表页展示)
    status: str = "pending"   # pending / running / done / failed
    summary: str = ""
    excel_path: str = ""
    error: str = ""
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    started_at: str = ""
    finished_at: str = ""
    duration_sec: float = 0.0
    scenario: str = ""        # workflow 最终 plan.scenario
    plan_summary: str = ""    # plan.ops 概要

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Job":
        return cls(**{k: d.get(k, "") for k in [
            "id", "username", "query", "ciphertext", "schema_summary", "status",
            "summary", "excel_path", "error", "created_at", "started_at",
            "finished_at", "scenario", "plan_summary",
        ]} | {"duration_sec": float(d.get("duration_sec", 0.0) or 0.0)})


class JobManager:
    """内存 + 文件双缓存的 job 队列。"""

    def __init__(self, root: Optional[Path] = None):
        self._root = root or HISTORY_DIR
        self._root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._jobs: dict[str, Job] = {}
        self._load_from_disk()

    # ----- 持久化 -----
    def _path_for(self, job_id: str) -> Path:
        return self._root / f"{job_id}.json"

    def _save(self, job: Job) -> None:
        try:
            self._path_for(job.id).write_text(
                json.dumps(job.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass  # 持久化失败不阻塞内存中的任务流转

    def _load_from_disk(self) -> None:
        if not self._root.exists():
            return
        for p in sorted(self._root.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                job = Job.from_dict(data)
                # 运行中的任务 → 进程重启后视为失败
                if job.status in ("pending", "running"):
                    job.status = "failed"
                    job.error = "主进程重启,任务中断"
                self._jobs[job.id] = job
            except Exception:
                continue

    # ----- CRUD -----
    def list_recent(self, username: Optional[str] = None, limit: int = 50) -> list[Job]:
        jobs = list(self._jobs.values())
        if username:
            jobs = [j for j in jobs if j.username == username]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.pop(job_id, None)
            if not job:
                return False
            try:
                self._path_for(job_id).unlink(missing_ok=True)
            except Exception:
                pass
            return True

    def submit(
        self,
        *,
        username: str,
        query: str,
        ciphertext: str,
        schema_summary: str,
        runner: Callable[["Job"], None],
    ) -> Job:
        """
        创建 job 并在工作线程跑 runner(job)。
        runner 负责更新 job.summary / excel_path / error / scenario,
        JobManager 负责状态机 + 时间戳 + 持久化。
        """
        with self._lock:
            jid = secrets.token_hex(6)
            while jid in self._jobs:
                jid = secrets.token_hex(6)
            job = Job(
                id=jid,
                username=username,
                query=query,
                ciphertext=ciphertext,
                schema_summary=schema_summary,
            )
            self._jobs[jid] = job
            self._save(job)

        def _worker():
            job.status = "running"
            job.started_at = datetime.now().isoformat(timespec="seconds")
            t0 = time.time()
            self._save(job)
            try:
                runner(job)
                job.status = "done" if not job.error else "failed"
            except Exception as e:
                job.status = "failed"
                job.error = f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1000:]}"
            finally:
                job.finished_at = datetime.now().isoformat(timespec="seconds")
                job.duration_sec = round(time.time() - t0, 2)
                self._save(job)

        threading.Thread(target=_worker, daemon=True, name=f"job-{jid}").start()
        return job
