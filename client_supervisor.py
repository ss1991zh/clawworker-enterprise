#!/usr/bin/env python3
"""
Clawworker **客户端**守护 —— 只看住 client(数据面 :8444):启动 / 健康探测 /
崩溃后指数退避重启。与 host 守护(supervisor.py)完全独立:独立状态文件
client_state.json、独立单例,co-located 也不冲突。

由客户端「设置 · 自启/运维」里的开机自启拉起(launchd/systemd/schtasks)。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from host import service_manager as sm  # noqa: E402

STATE = sm.STATE_DIR / "client_state.json"
LOG = sm.STATE_DIR / "client_supervisor.log"
POLL_SEC = 3.0
BACKOFF_BASE, BACKOFF_FACTOR, BACKOFF_CAP = 2.0, 2.0, 30.0
STABLE_RESET_SEC = 30.0
HEARTBEAT_STALE_SEC = 20

_running = True


def _now() -> float:
    return time.time()


def _log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] client-supervisor: {msg}", flush=True)


def _read() -> dict:
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _write(d: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)


def _other_alive() -> bool:
    d = _read()
    pid, ts = d.get("pid"), d.get("ts", 0)
    if not sm.pid_alive(pid):
        return False
    return not (ts and (_now() - ts) > HEARTBEAT_STALE_SEC)


def _spawn() -> subprocess.Popen:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    logf = open(sm.STATE_DIR / "client.log", "a", encoding="utf-8")  # noqa: SIM115
    return subprocess.Popen(
        sm.service_run_argv("client"),
        cwd=str(sm.PROJECT_DIR), env={**os.environ, "AGENT_BACKEND": "real"},
        stdout=logf, stderr=logf,
        start_new_session=(sm.current_platform() != "windows"),
    )


def _sig(signum, frame):  # noqa: ARG001
    global _running
    _running = False


def main() -> int:
    if _other_alive():
        d = _read()
        _log(f"已有客户端守护在运行(pid={d.get('pid')}),本进程退出")
        return 0
    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    started_at = _now()
    _log(f"启动 · pid={os.getpid()} · 托管 client(:{sm.SERVICES['client']['port']})")
    proc = None
    restarts = 0
    last_start = 0.0
    backoff = BACKOFF_BASE
    healthy_since = 0.0
    last_restart = None

    while _running:
        if proc is not None and proc.poll() is not None:
            _log(f"client 子进程退出(code={proc.returncode}),将重启")
            proc = None
        healthy = sm.probe_port(sm.SERVICES["client"]["port"])
        if healthy:
            if healthy_since == 0.0:
                healthy_since = _now()
            if _now() - healthy_since >= STABLE_RESET_SEC:
                backoff = BACKOFF_BASE
        else:
            healthy_since = 0.0
            if not (proc is not None and proc.poll() is None) and (_now() - last_start >= backoff):
                try:
                    proc = _spawn()
                    restarts += 1
                    last_start = _now()
                    last_restart = time.strftime("%Y-%m-%d %H:%M:%S")
                    backoff = min(backoff * BACKOFF_FACTOR, BACKOFF_CAP)
                    _log(f"client 已拉起(pid={proc.pid},第 {restarts} 次)")
                except Exception as e:  # noqa: BLE001
                    _log(f"client 拉起失败: {e}")
        _write({
            "pid": os.getpid(), "started_at": started_at, "ts": _now(),
            "client": {"pid": proc.pid if proc else None, "restarts": restarts,
                       "last_restart": last_restart,
                       "healthy": sm.probe_port(sm.SERVICES["client"]["port"]),
                       "backoff": round(backoff, 1)},
        })
        slept = 0.0
        while _running and slept < POLL_SEC:
            time.sleep(0.25)
            slept += 0.25

    # 退出:默认保留子进程(界面不掉线),只停守护
    if bool(_read().get("stop_kill_children", False)) and proc and proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        time.sleep(1.0)
    st = _read()
    st["pid"] = None
    st["ts"] = _now()
    st.pop("stop_kill_children", None)
    _write(st)
    _log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
