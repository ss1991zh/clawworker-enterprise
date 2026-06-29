"""
Clawworker 桌面启动器 —— 双击桌面图标时运行。

做三件事:
  1. 看对应角色的服务端口是否已在跑(管理端 :8443 / 用户端 :8444);
  2. 没在跑就拉起 supervisor(只托管这个角色、后台无窗口、崩溃自愈);
  3. 等端口就绪,用默认浏览器打开界面。

由桌面快捷方式以 pythonw.exe(无控制台窗口)调用:
    pythonw clawworker_launch.py admin     # 管理端
    pythonw clawworker_launch.py client    # 用户端
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

ROLES = {
    "admin":  {"svc": "host",   "port": 8443, "url": "http://localhost:8443", "label": "管理端"},
    "client": {"svc": "client", "port": 8444, "url": "http://localhost:8444", "label": "用户端"},
}


def _find_project_dir(start: Path) -> Path:
    """从启动器所在位置向上找含 supervisor.py 的项目根。"""
    p = start
    for _ in range(6):
        if (p / "supervisor.py").exists():
            return p
        p = p.parent
    return start


def _port_up(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.6):
            return True
    except OSError:
        return False


def _start_supervisor(project_dir: Path, svc: str) -> None:
    """后台、无窗口、脱离父进程地拉起 supervisor,只托管指定角色。"""
    env = {**os.environ, "AGENT_BACKEND": "real", "CLAWWORKER_MANAGED_SERVICES": svc}
    kwargs = dict(cwd=str(project_dir), env=env,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if os.name == "nt":
        # 无控制台窗口 + 脱离,关掉启动器也不杀服务
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen([sys.executable, str(project_dir / "supervisor.py")], **kwargs)


def main() -> int:
    role = (sys.argv[1] if len(sys.argv) > 1 else "client").strip().lower()
    cfg = ROLES.get(role, ROLES["client"])
    project_dir = _find_project_dir(Path(__file__).resolve().parent)

    if not _port_up(cfg["port"]):
        _start_supervisor(project_dir, cfg["svc"])
        # 等服务起来(首次启动含密钥初始化,给足 ~40s)
        for _ in range(80):
            if _port_up(cfg["port"]):
                break
            time.sleep(0.5)

    webbrowser.open(cfg["url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
