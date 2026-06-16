#!/usr/bin/env python3
"""
Clawworker 守护进程 —— 把 host(:8443)+ client(:8444)托管起来:
  · 启动时拉起两个服务(若端口已被本机健康实例占用 → 直接接管,不重复拉起)
  · 每 3s 健康探测;子进程崩溃 / 端口不通 → 指数退避后自动重启
  · 持续写心跳到 ~/.agent-system/supervisor/state.json(供 admin 状态页读取)
  · 收到 SIGTERM/SIGINT → 优雅退出(默认保留子服务,使 admin 不掉线)

三平台一致:这套崩溃自愈逻辑是纯 Python,不依赖各 OS 的服务管理器。
OS 的开机自启(launchd/systemd/schtasks)只负责"登录时把本守护跑起来",
并在 mac/linux 上顺带为本守护自身做 KeepAlive(见 host/service_manager.py)。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# 允许 `python supervisor.py` 直接运行(把项目根放进 sys.path)
sys.path.insert(0, str(Path(__file__).resolve().parent))

from host import service_manager as sm  # noqa: E402

POLL_SEC = 3.0
BACKOFF_BASE = 2.0
BACKOFF_FACTOR = 2.0
BACKOFF_CAP = 30.0
STABLE_RESET_SEC = 30.0          # 健康持续这么久 → 退避重置


_running = True


def _now() -> float:
    return time.time()


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] supervisor: {msg}"
    print(line, flush=True)


def _spawn(svc_key: str) -> subprocess.Popen:
    logp = sm.STATE_DIR / f"{svc_key}.log"
    logp.parent.mkdir(parents=True, exist_ok=True)
    logf = open(logp, "a", encoding="utf-8")  # noqa: SIM115
    env = {**os.environ, "AGENT_BACKEND": "real"}
    return subprocess.Popen(
        sm.service_run_argv(svc_key),
        cwd=str(sm.PROJECT_DIR),
        env=env,
        stdout=logf,
        stderr=logf,
        start_new_session=(sm.current_platform() != "windows"),
    )


def _handle_signal(signum, frame):  # noqa: ARG001
    global _running
    _running = False
    _log(f"收到信号 {signum},准备退出")


def main() -> int:
    # ---- 单例守护:已有健康 supervisor 在跑就退出 ----
    running, pid = sm.supervisor_running()
    if running and pid and pid != os.getpid():
        _log(f"已有 supervisor 在运行(pid={pid}),本进程退出")
        return 0

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    started_at = _now()
    managed = sm.managed_service_keys()
    _log(f"启动 · pid={os.getpid()} · 托管 {managed}")

    # 每个服务的运行态
    mgr: dict[str, dict] = {
        key: {"proc": None, "restarts": 0, "last_start": 0.0,
              "backoff": BACKOFF_BASE, "healthy_since": 0.0, "last_restart": None}
        for key in managed
    }

    def _write_state():
        services = {}
        for key, m in mgr.items():
            proc = m["proc"]
            services[key] = {
                "pid": proc.pid if proc else None,
                "restarts": m["restarts"],
                "last_restart": m["last_restart"],
                "healthy": sm.probe_port(sm.SERVICES[key]["port"]),
                "backoff": round(m["backoff"], 1),
            }
        sm.write_state({
            "pid": os.getpid(),
            "started_at": started_at,
            "ts": _now(),
            "services": services,
        })

    while _running:
        for key in managed:
            svc = sm.SERVICES[key]
            m = mgr[key]
            proc = m["proc"]

            # 1) 我们 spawn 的子进程退出了?
            if proc is not None and proc.poll() is not None:
                _log(f"{key} 子进程退出(code={proc.returncode}),将重启")
                m["proc"] = None
                proc = None

            healthy = sm.probe_port(svc["port"])

            if healthy:
                # 健康:可能是我们拉的,也可能是已存在的实例(接管监控)
                if m["healthy_since"] == 0.0:
                    m["healthy_since"] = _now()
                # 稳定足够久 → 退避重置
                if _now() - m["healthy_since"] >= STABLE_RESET_SEC:
                    m["backoff"] = BACKOFF_BASE
                continue

            # 不健康
            m["healthy_since"] = 0.0
            if proc is not None and proc.poll() is None:
                continue                      # 刚 spawn,还在启动中,等

            # 需要(重新)拉起 —— 退避控制,避免崩溃风暴
            if _now() - m["last_start"] < m["backoff"]:
                continue
            try:
                m["proc"] = _spawn(key)
                m["restarts"] += 1
                m["last_start"] = _now()
                m["last_restart"] = time.strftime("%Y-%m-%d %H:%M:%S")
                m["backoff"] = min(m["backoff"] * BACKOFF_FACTOR, BACKOFF_CAP)
                _log(f"{key} 已拉起(pid={m['proc'].pid},第 {m['restarts']} 次,"
                     f"下次退避≤{m['backoff']:.0f}s)")
            except Exception as e:  # noqa: BLE001
                _log(f"{key} 拉起失败: {e}")

        _write_state()
        # 分片 sleep,信号能及时打断
        slept = 0.0
        while _running and slept < POLL_SEC:
            time.sleep(0.25)
            slept += 0.25

    # ---- 退出:按 stop_kill_children 决定是否带走子进程 ----
    kill_children = bool(sm.read_state().get("stop_kill_children", False))
    if kill_children:
        for key, m in mgr.items():
            proc = m["proc"]
            if proc and proc.poll() is None:
                _log(f"停止 {key}(pid={proc.pid})")
                try:
                    proc.terminate()
                except OSError:
                    pass
        time.sleep(1.0)
    else:
        _log("退出但保留子服务(host/client 继续运行;崩溃自愈已停)")

    # 清掉自己的 pid,让 admin 看到"未守护";保留 services 历史
    st = sm.read_state()
    st["pid"] = None
    st["ts"] = _now()
    st.pop("stop_kill_children", None)
    sm.write_state(st)
    _log("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
