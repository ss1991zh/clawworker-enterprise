"""
跨平台服务管理 —— 开机自启 + 健康监控 + 崩溃自愈的底座。

设计:
- 两个被托管服务:host(控制面 :8443)+ client(数据面 :8444)。
- **统一守护模型**:开机自启只负责拉起**一个** supervisor 进程;
  supervisor 负责 spawn / 健康探测 / 崩溃重启两个服务(纯 Python 循环,
  三平台行为一致 —— 见 supervisor.py)。
- 平台相关的只有"开机时把 supervisor 跑起来"这一件事:
    · macOS   → LaunchAgent plist(RunAtLoad + KeepAlive)
    · Linux   → systemd --user unit(Restart=always)
    · Windows → 计划任务(schtasks /SC ONLOGON)
  其中 mac/linux 的 OS 机制顺带给 supervisor 本身做 KeepAlive;
  子服务(host/client)的崩溃重启由 supervisor 兜底,三平台一致。

本模块**只做**:平台探测 / 端口健康探测 / 读 supervisor 状态 /
开机自启的安装·卸载·查询。不含监控循环(那在 supervisor.py)。
无 FastAPI 依赖,可被 admin 路由和 supervisor 同时复用。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# 路径 / 常量
# ---------------------------------------------------------------------------

PROJECT_DIR = Path(__file__).resolve().parent.parent          # agent-system/
SUPERVISOR_PY = PROJECT_DIR / "supervisor.py"
STATE_DIR = Path.home() / ".agent-system" / "supervisor"
STATE_FILE = STATE_DIR / "state.json"
SUP_LOG = STATE_DIR / "supervisor.log"

# 服务定义(supervisor 与 admin 共用这一份事实来源)。
# 注意:host(控制面 :8443)部署在中心机器(admin 所在机);
#       client(数据面 :8444)部署在**各终端用户机器**上,各自托管。
# 因此一台机器的守护**只托管它本地承担的角色**(见 managed_service_keys)。
SERVICES: dict[str, dict] = {
    "host": {
        "label": "控制面 Host",
        "app": "host.server:app",
        "bind": "0.0.0.0",
        "port": 8443,
    },
    "client": {
        "label": "数据面 Client",
        "app": "client.webui:app",
        "bind": "127.0.0.1",
        "port": 8444,
    },
}


def managed_service_keys() -> list[str]:
    """本机守护要托管的服务键。

    默认只托管 **host**(:8443)—— admin/控制面机器的角色。
    client(:8444)运行在各终端机器上,由那台机器自己的守护托管。
    终端机器部署时设环境变量切换,例如:
        CLAWWORKER_MANAGED_SERVICES=client
    也支持逗号分隔多角色(单机一体化部署):host,client
    """
    raw = os.environ.get("CLAWWORKER_MANAGED_SERVICES", "").strip()
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip() in SERVICES]
        if keys:
            return keys
    return ["host"]

# 开机自启标识
LAUNCHD_LABEL = "com.clawworker.supervisor"
SYSTEMD_UNIT = "clawworker-supervisor"
WIN_TASK_NAME = "Clawworker Supervisor"

# 心跳新鲜度:supervisor 每轮写 ts;超过这个秒数视为已死
HEARTBEAT_STALE_SEC = 20


def current_platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("win"):
        return "windows"
    return "linux"


def _py_exe() -> str:
    """后台运行用的 python。Windows 优先 pythonw(无控制台窗口)。"""
    exe = sys.executable or "python3"
    if current_platform() == "windows":
        cand = Path(exe).with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return exe


def service_run_argv(svc_key: str) -> list[str]:
    """拉起单个服务的命令行(uvicorn)。supervisor 用它 spawn 子进程。
    host(控制面)启 HTTPS:证书不存在则自动生成(自签 + 指纹锁定 TOFU)。
    client(:8444)只监听回环,保持 HTTP(本机无嗅探面,且简化本地访问)。"""
    svc = SERVICES[svc_key]
    argv = [
        _py_exe(), "-m", "uvicorn", svc["app"],
        "--host", svc["bind"], "--port", str(svc["port"]),
        "--timeout-keep-alive", "75",
    ]
    if svc_key == "host":
        try:
            from host import tls_cert
            cert_path, key_path, _fp = tls_cert.ensure_cert()
            argv += ["--ssl-keyfile", str(key_path), "--ssl-certfile", str(cert_path)]
        except Exception:  # noqa: BLE001 —— 证书生成失败则退回 HTTP,不阻断启动(日志会记)
            pass
    return argv


# ---------------------------------------------------------------------------
# 健康探测 / 进程存活
# ---------------------------------------------------------------------------


def probe_port(port: int, host: str = "127.0.0.1", timeout: float = 0.6) -> bool:
    """TCP 连通即视为在跑(uvicorn 监听端口)。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def pid_alive(pid: Optional[int]) -> bool:
    if not pid or pid <= 0:
        return False
    plat = current_platform()
    if plat == "windows":
        try:
            import ctypes  # noqa: PLC0415
            PROCESS_QUERY_LIMITED = 0x1000
            h = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED, False, int(pid))
            if not h:
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True       # 存在但无权限 → 仍算活着
    except OSError:
        return False


# ---------------------------------------------------------------------------
# supervisor 状态(state.json)
# ---------------------------------------------------------------------------


def read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def write_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def supervisor_running() -> tuple[bool, Optional[int]]:
    """守护进程是否在跑 = pid 存活 且 心跳新鲜。返回 (running, pid)。"""
    st = read_state()
    pid = st.get("pid")
    ts = st.get("ts", 0)
    if not pid_alive(pid):
        return False, pid
    if ts and (time.time() - ts) > HEARTBEAT_STALE_SEC:
        return False, pid           # 进程在,但心跳停了(卡死)→ 视为不健康
    return True, pid


def status_snapshot() -> dict:
    """admin 状态页 / status.json 的统一数据源。"""
    sup_running, sup_pid = supervisor_running()
    st = read_state()
    sup_services = st.get("services", {})

    services = []
    for key in managed_service_keys():
        svc = SERVICES[key]
        healthy = probe_port(svc["port"], host="127.0.0.1")
        ext = sup_services.get(key, {})
        services.append({
            "key": key,
            "label": svc["label"],
            "port": svc["port"],
            "healthy": healthy,
            "pid": ext.get("pid"),
            "restarts": ext.get("restarts", 0),
            "last_restart": ext.get("last_restart"),
            "managed": bool(ext),         # supervisor 是否在管它
        })

    auto = autostart_status()
    return {
        "platform": current_platform(),
        "supervisor": {
            "running": sup_running,
            "pid": sup_pid,
            "started_at": st.get("started_at"),
            "ts": st.get("ts"),
        },
        "autostart": auto,
        "services": services,
        "now": time.time(),
    }


# ---------------------------------------------------------------------------
# 开机自启:安装 / 卸载 / 查询(平台分支)
# ---------------------------------------------------------------------------


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"


def autostart_status() -> dict:
    """开机自启是否已安装。返回 {installed, kind, path, detail}。"""
    plat = current_platform()
    if plat == "darwin":
        p = _launchd_plist_path()
        return {"installed": p.exists(), "kind": "launchd", "path": str(p),
                "detail": "LaunchAgent · 登录自启 + 失败自愈"}
    if plat == "linux":
        p = _systemd_unit_path()
        return {"installed": p.exists(), "kind": "systemd", "path": str(p),
                "detail": "systemd --user · Restart=on-failure"}
    if plat == "windows":
        return {"installed": _win_autostart_installed(), "kind": "registry-run",
                "path": rf"HKCU\{_WIN_RUN_SUBKEY}\{WIN_TASK_NAME}",
                "detail": "登录自启 · HKCU Run 键(免管理员)"}
    return {"installed": False, "kind": "unknown", "path": "", "detail": ""}


def _run(argv: list[str], **kw) -> subprocess.CompletedProcess:
    # Windows:父进程是 pythonw(无控制台)时,拉起 schtasks/taskkill 等控制台程序会
    # 新建一个一闪而过的黑窗。ops 页每 4s 轮询 status → autostart_status → schtasks,
    # 会造成周期性闪窗 —— 加 CREATE_NO_WINDOW 彻底消除(非 Windows 上该常量为 0,无副作用)。
    if current_platform() == "windows":
        kw.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return subprocess.run(argv, capture_output=True, text=True, **kw)


def _env_for_service() -> dict[str, str]:
    env = {"AGENT_BACKEND": "real"}
    # 把当前 PATH 带上,确保 launchd/systemd 能找到 python 依赖
    if os.environ.get("PATH"):
        env["PATH"] = os.environ["PATH"]
    # 角色(托管哪些服务)固化进开机自启项,使重启后行为一致
    role = os.environ.get("CLAWWORKER_MANAGED_SERVICES", "").strip()
    if role:
        env["CLAWWORKER_MANAGED_SERVICES"] = role
    return env


# ---- macOS: launchd ----

def _mac_plist_xml() -> str:
    env = _env_for_service()
    env_xml = "".join(
        f"      <key>{k}</key><string>{v}</string>\n" for k, v in env.items()
    )
    SUP_LOG.parent.mkdir(parents=True, exist_ok=True)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{_py_exe()}</string>
    <string>{SUPERVISOR_PY}</string>
  </array>
  <key>WorkingDirectory</key><string>{PROJECT_DIR}</string>
  <key>EnvironmentVariables</key>
  <dict>
{env_xml}  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key><false/>
  </dict>
  <key>StandardOutPath</key><string>{SUP_LOG}</string>
  <key>StandardErrorPath</key><string>{SUP_LOG}</string>
</dict>
</plist>
"""


def _mac_install() -> str:
    p = _launchd_plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_mac_plist_xml(), encoding="utf-8")
    _run(["launchctl", "unload", str(p)])            # 幂等:先卸再装
    r = _run(["launchctl", "load", "-w", str(p)])
    if r.returncode != 0:
        raise RuntimeError(f"launchctl load 失败: {r.stderr.strip() or r.stdout.strip()}")
    return f"已写入 {p.name} 并 launchctl load(开机/登录自启 + 崩溃自愈)"


def _mac_uninstall() -> str:
    p = _launchd_plist_path()
    if p.exists():
        _run(["launchctl", "unload", "-w", str(p)])
        p.unlink()
        return f"已 launchctl unload 并删除 {p.name}"
    return "未安装(无 plist)"


# ---- Linux: systemd --user ----

def _systemd_unit_text() -> str:
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in _env_for_service().items())
    return f"""[Unit]
Description=Clawworker Enterprise Supervisor (host+client watchdog)
After=network.target

[Service]
Type=simple
WorkingDirectory={PROJECT_DIR}
{env_lines}
ExecStart={_py_exe()} {SUPERVISOR_PY}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def _linux_install() -> str:
    p = _systemd_unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(_systemd_unit_text(), encoding="utf-8")
    _run(["systemctl", "--user", "daemon-reload"])
    r = _run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.service"])
    if r.returncode != 0:
        raise RuntimeError(f"systemctl enable 失败: {r.stderr.strip() or r.stdout.strip()}")
    # 提示开机(非登录)自启需 linger
    _run(["loginctl", "enable-linger", os.environ.get("USER", "")])
    return f"已写入 {p.name} 并 systemctl --user enable --now"


def _linux_uninstall() -> str:
    p = _systemd_unit_path()
    if p.exists():
        _run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.service"])
        p.unlink()
        _run(["systemctl", "--user", "daemon-reload"])
        return f"已 systemctl disable 并删除 {p.name}"
    return "未安装(无 unit)"


# ---- Windows: schtasks ----

# 登录自启走 HKCU 的 Run 键:纯用户态,免管理员。
# (schtasks /SC ONLOGON 在很多机器上 /Create 需要提权 → "拒绝访问",故弃用。)
# 登录时由 explorer 在用户会话里执行该命令,继承用户环境变量(含 CLAWWORKER_MANAGED_SERVICES),
# 用 pythonw 启动 supervisor → 无窗口。
_WIN_RUN_SUBKEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _win_autostart_installed() -> bool:
    import winreg  # noqa: PLC0415  Windows-only
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_SUBKEY) as k:
            winreg.QueryValueEx(k, WIN_TASK_NAME)
        return True
    except OSError:
        return False


def _win_install() -> str:
    import winreg  # noqa: PLC0415
    cmd = f'"{_py_exe()}" "{SUPERVISOR_PY}"'
    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_SUBKEY) as k:
            winreg.SetValueEx(k, WIN_TASK_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as e:
        raise RuntimeError(f"写入登录自启注册表失败: {e}") from e
    return f"已注册登录自启「{WIN_TASK_NAME}」(HKCU Run · 免管理员 · 登录时启动 supervisor)"


def _win_uninstall() -> str:
    import winreg  # noqa: PLC0415
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_SUBKEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, WIN_TASK_NAME)
    except FileNotFoundError:
        return "未安装(无该自启项)"
    except OSError as e:
        return f"卸载失败: {e}"
    return f"已移除登录自启「{WIN_TASK_NAME}」"


def install_autostart() -> str:
    plat = current_platform()
    if plat == "darwin":
        return _mac_install()
    if plat == "linux":
        return _linux_install()
    if plat == "windows":
        msg = _win_install()
        ensure_supervisor_running()       # schtasks 不会立即启动,这里补一脚
        return msg
    raise RuntimeError(f"不支持的平台: {plat}")


def uninstall_autostart() -> str:
    plat = current_platform()
    if plat == "darwin":
        return _mac_uninstall()
    if plat == "linux":
        return _linux_uninstall()
    if plat == "windows":
        return _win_uninstall()
    raise RuntimeError(f"不支持的平台: {plat}")


# ---------------------------------------------------------------------------
# 手动启停 supervisor(不依赖开机自启,也能用)
# ---------------------------------------------------------------------------


def spawn_supervisor_detached() -> int:
    """脱离父进程拉起 supervisor。返回新进程 pid(非守护内部记录的 pid)。"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    plat = current_platform()
    kw: dict = {"cwd": str(PROJECT_DIR), "env": {**os.environ, **_env_for_service()}}
    logf = open(SUP_LOG, "a", encoding="utf-8")  # noqa: SIM115
    kw["stdout"] = logf
    kw["stderr"] = logf
    if plat == "windows":
        kw["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    proc = subprocess.Popen([_py_exe(), str(SUPERVISOR_PY)], **kw)
    return proc.pid


def ensure_supervisor_running() -> bool:
    """若 supervisor 未在跑则拉起。返回是否执行了启动。"""
    running, _ = supervisor_running()
    if running:
        return False
    spawn_supervisor_detached()
    return True


def restart_service(key: str) -> str:
    """托管重启单个服务:杀掉受管子进程,supervisor 会在数秒内拉起新实例。
    需要 supervisor 在跑,且该服务确实由它托管(state 里有 pid)。"""
    if key not in SERVICES:
        raise RuntimeError(f"未知服务: {key}")
    running, _ = supervisor_running()
    if not running:
        raise RuntimeError("需先启用守护(supervisor)才能托管重启")
    pid = read_state().get("services", {}).get(key, {}).get("pid")
    if not pid_alive(pid):
        raise RuntimeError(f"{SERVICES[key]['label']} 当前无受管进程(可能是外部手动启动的实例,守护不会接管其重启)")
    try:
        if current_platform() == "windows":
            _run(["taskkill", "/PID", str(pid), "/F"])
        else:
            os.kill(int(pid), 15)
    except OSError as e:
        raise RuntimeError(f"重启失败: {e}") from e
    return f"已请求重启「{SERVICES[key]['label']}」,守护将在数秒内拉起新实例"


def stop_supervisor(kill_children: bool = False) -> str:
    """停掉 supervisor。默认**不杀子服务**(host/client 继续跑,admin 不掉线),
    只是停止守护(崩溃自愈随之失效,直到重新启用)。"""
    running, pid = supervisor_running()
    if not running or not pid:
        return "supervisor 未在运行"
    # 通过状态文件告诉 supervisor:退出时是否带走子进程
    st = read_state()
    st["stop_kill_children"] = bool(kill_children)
    write_state(st)
    try:
        if current_platform() == "windows":
            _run(["taskkill", "/PID", str(pid), "/T" if kill_children else "/F", "/F"])
        else:
            os.kill(int(pid), 15)  # SIGTERM → supervisor 优雅退出
    except OSError as e:
        return f"停止失败: {e}"
    return f"已向 supervisor(pid={pid})发送停止信号"
