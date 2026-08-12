#!/usr/bin/env python3
"""
Clawworker 守护进程 —— 托管 host(本机 HTTP :8442 + 局域网 HTTPS :8443)
与 client(本机 HTTP :8444):
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

POLL_SEC = 1.0
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


def _handoff_or_replace_existing(want: list[str]) -> bool:
    """处理共享状态里已存在的守护；返回 True 表示当前进程应退出。

    同协议的新守护可以动态接管角色，直接把请求交给它。旧协议守护不认识
    :8442/:8443 双入口和角色并集，必须连同旧子服务一起替换。
    """
    running, pid = sm.supervisor_running(require_compatible=False)
    if not running or not pid or pid == os.getpid():
        return False
    if sm.supervisor_state_compatible():
        merged = sm.add_desired(want)
        _log(
            f"已有兼容 supervisor 在运行(pid={pid}),已请求其接管 {want}"
            f"(期望集合={merged}),本进程退出"
        )
        return True

    _log(f"发现不兼容旧 supervisor(pid={pid}),正在终止旧守护及其子服务")
    if not sm.terminate_incompatible_supervisor(pid):
        raise RuntimeError(f"无法终止旧 supervisor(pid={pid}),请注销 Windows 后重试")
    wanted = sm.set_desired(want)
    _log(f"旧 supervisor 已退出,由当前安装目录接管(期望集合={wanted})")
    return False


def main() -> int:
    # ---- 单例守护:已有健康 supervisor 在跑就退出 ----
    # 退出前先把本进程想托管的角色并进「期望集合」—— 在跑的那个每轮会读它并接管。
    # 否则角色分片 + 单例锁会互相堵死:先起的只管 client,后来点「管理端」图标起的
    # supervisor 撞锁即退,没人拉 host,表现为点图标毫无反应。
    want = sm.managed_service_keys()
    try:
        if _handoff_or_replace_existing(want):
            return 0
    except RuntimeError as exc:
        _log(f"启动失败: {exc}")
        return 2

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    started_at = _now()
    # 本进程角色 ∪ 历史期望集合(上次别的角色请求过、重启后不该丢)
    managed = sm.add_desired(want)
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
                "healthy": sm.service_healthy(key),
                "backoff": round(m["backoff"], 1),
            }
        sm.write_state({
            "protocol": sm.SUPERVISOR_PROTOCOL,
            "project_dir": str(sm.PROJECT_DIR),
            "supervisor_py": str(sm.SUPERVISOR_PY),
            "pid": os.getpid(),
            "started_at": started_at,
            "ts": _now(),
            "services": services,
        })

    while _running:
        # 接管新角色:别的 launcher(如桌面「管理端」图标)撞上单例锁退出前,
        # 会把它想要的角色写进期望集合 —— 这里读到就纳入托管,下一轮即拉起。
        for key in sm.read_desired():
            if key not in mgr:
                managed.append(key)
                mgr[key] = {"proc": None, "restarts": 0, "last_start": 0.0,
                            "backoff": BACKOFF_BASE, "healthy_since": 0.0,
                            "last_restart": None}
                _log(f"接管新角色 {key}(来自期望集合)")
        for key in managed:
            svc = sm.SERVICES[key]
            m = mgr[key]
            proc = m["proc"]

            # 1) 我们 spawn 的子进程退出了?
            if proc is not None and proc.poll() is not None:
                _log(f"{key} 子进程退出(code={proc.returncode}),将重启")
                m["proc"] = None
                proc = None

            healthy = sm.service_healthy(key)

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
