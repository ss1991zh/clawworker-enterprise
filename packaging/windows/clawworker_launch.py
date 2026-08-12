"""
Clawworker 桌面启动器 —— 双击桌面图标时运行。

做三件事:
  1. 看对应角色的本机 HTTP 端口是否已在跑(管理端 :8442 / 用户端 :8444);
  2. 没在跑就拉起 supervisor(只托管这个角色、后台无窗口、崩溃自愈);
  3. 等端口就绪：用户端打开独立桌面窗口，管理端仍用默认浏览器。

由桌面快捷方式以 pythonw.exe(无控制台窗口)调用:
    pythonw clawworker_launch.py admin     # 管理端
    pythonw clawworker_launch.py client    # 用户端
"""
from __future__ import annotations

import os
import http.client
import socket
import subprocess
import sys
import time
import webbrowser
import importlib.util
from pathlib import Path
from urllib.parse import urlsplit

ROLES = {
    # 本机浏览器只走 loopback HTTP,不再要求每台终端安装自签根证书。
    # host 另在 :8443 提供局域网 HTTPS,由同一个 gateway 进程托管。
    "admin": {
        "svc": "host",
        "port": 8442,
        "url": "http://127.0.0.1:8442/admin",
        "ready_url": "http://127.0.0.1:8442/admin/login",
        "label": "管理端",
    },
    "client": {
        "svc": "client",
        "port": 8444,
        "url": "http://127.0.0.1:8444",
        "ready_url": "http://127.0.0.1:8444/healthz",
        "label": "用户端",
    },
}

STATE_DIR = Path.home() / ".agent-system" / "supervisor"
SUPERVISOR_LOG = STATE_DIR / "supervisor.log"
HOST_LOG = STATE_DIR / "host.log"
CLIENT_LOG = STATE_DIR / "client.log"
LAUNCHER_LOG = STATE_DIR / "launcher.log"
STARTUP_TIMEOUT_SEC = 90.0
HTTP_PROBE_TIMEOUT_SEC = 2.0


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


def _http_ready(url: str) -> bool:
    """直连本机 HTTP，不继承系统代理，避免 loopback 请求被代理后误报超时。"""
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
        return False
    port = parsed.port or 80
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    connection = http.client.HTTPConnection(
        parsed.hostname,
        port,
        timeout=HTTP_PROBE_TIMEOUT_SEC,
    )
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        response = connection.getresponse()
        return 200 <= response.status < 500
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()


def _log(message: str) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with LAUNCHER_LOG.open("a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n")
    except OSError:
        pass


def _error_message(label: str, svc: str) -> str:
    service_log = CLIENT_LOG if svc == "client" else HOST_LOG
    return (
        f"{label}未能在 {int(STARTUP_TIMEOUT_SEC)} 秒内启动。\n\n"
        f"请查看日志：\n{SUPERVISOR_LOG}\n{service_log}\n{LAUNCHER_LOG}\n\n"
        "可重新双击图标；若仍失败，请把上述日志发给管理员。"
    )


def _show_error(label: str, svc: str) -> None:
    message = _error_message(label, svc)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, f"Clawworker {label}启动失败", 0x10)
            return
        except Exception:
            pass
    _log(message.replace("\n", " | "))


def _start_supervisor(project_dir: Path, svc: str) -> None:
    """后台、无窗口、脱离父进程地拉起 supervisor,只托管指定角色。"""
    env = {**os.environ, "AGENT_BACKEND": "real", "CLAWWORKER_MANAGED_SERVICES": svc}
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logf = SUPERVISOR_LOG.open("a", encoding="utf-8")
    kwargs = dict(cwd=str(project_dir), env=env,
                  stdout=logf, stderr=subprocess.STDOUT)
    if os.name == "nt":
        # 无控制台窗口 + 脱离,关掉启动器也不杀服务
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                                   | getattr(subprocess, "DETACHED_PROCESS", 0))
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([sys.executable, str(project_dir / "supervisor.py")], **kwargs)
    finally:
        logf.close()


def _open_client_desktop(project_dir: Path, url: str) -> bool:
    """延迟载入桌面窗口模块，避免管理端启动依赖 pywebview。"""
    module_path = project_dir / "packaging" / "windows" / "clawworker_desktop.py"
    spec = importlib.util.spec_from_file_location("clawworker_desktop_runtime", module_path)
    if not spec or not spec.loader:
        _log(f"桌面窗口模块不存在或无法载入 · {module_path}")
        return False
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return bool(module.open_client_window(
        url,
        icon_path=project_dir / "packaging" / "windows" / "clawworker.ico",
        state_dir=STATE_DIR,
        log=_log,
    ))


def main() -> int:
    role = (sys.argv[1] if len(sys.argv) > 1 else "client").strip().lower()
    cfg = ROLES.get(role, ROLES["client"])
    browser_mode = "--browser" in sys.argv[2:]
    project_dir = _find_project_dir(Path(__file__).resolve().parent)

    _log(f"启动 {cfg['label']} · project={project_dir} · url={cfg['url']}")
    if not _http_ready(cfg["ready_url"]):
        if not _port_up(cfg["port"]):
            _start_supervisor(project_dir, cfg["svc"])
        else:
            _log(
                f"{cfg['label']}端口已监听，等待 HTTP 服务就绪，"
                "不重复启动 supervisor"
            )
        # 以墙钟总期限为准。旧版按 120 次循环计时,但每次 HTTP 探测还可能
        # 阻塞 1 秒,标称 30 秒实际可拖到约 150 秒。
        deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if _http_ready(cfg["ready_url"]):
                break
            time.sleep(0.25)

    if _http_ready(cfg["ready_url"]):
        if role == "client" and not browser_mode:
            if _open_client_desktop(project_dir, cfg["url"]):
                return 0
            _log("桌面窗口不可用，回退到默认浏览器")
        _log(f"{cfg['label']} 已就绪，用默认浏览器打开 {cfg['url']}")
        webbrowser.open(cfg["url"])
        return 0

    _log(f"{cfg['label']} 启动超时 · port_up={_port_up(cfg['port'])}")
    _show_error(cfg["label"], cfg["svc"])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
