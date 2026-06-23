"""
客户端(数据面 :8444)自身的开机自启 + 崩溃重启管理 —— 由客户端「设置 · 自启」调用。

与 host 的 service_manager 完全独立:独立的开机自启项(label/unit/task)、独立的
守护脚本 client_supervisor.py、独立的状态文件 client_state.json。co-located 不冲突。

复用 host.service_manager 里的平台无关工具(平台探测 / 端口探测 / pid 存活 /
python 路径 / 子进程跑命令 / 环境变量),只在这里实现"客户端角色"的安装与状态。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Optional

from host import service_manager as sm

PROJECT_DIR = sm.PROJECT_DIR
CLIENT_SUP_PY = PROJECT_DIR / "client_supervisor.py"
STATE_FILE = sm.STATE_DIR / "client_state.json"
SUP_LOG = sm.STATE_DIR / "client_supervisor.log"
CLIENT_PORT = sm.SERVICES["client"]["port"]

LAUNCHD_LABEL = "com.clawworker.client"
SYSTEMD_UNIT = "clawworker-client"
WIN_TASK_NAME = "Clawworker Client"
HEARTBEAT_STALE_SEC = 20


def _read_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _write_state(d: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def supervisor_running() -> tuple[bool, Optional[int]]:
    st = _read_state()
    pid, ts = st.get("pid"), st.get("ts", 0)
    if not sm.pid_alive(pid):
        return False, pid
    if ts and (time.time() - ts) > HEARTBEAT_STALE_SEC:
        return False, pid
    return True, pid


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_UNIT}.service"


def autostart_status() -> dict:
    plat = sm.current_platform()
    if plat == "darwin":
        p = _launchd_plist_path()
        return {"installed": p.exists(), "kind": "launchd", "path": str(p),
                "detail": "LaunchAgent · 登录自启 + 失败自愈"}
    if plat == "linux":
        p = _systemd_unit_path()
        return {"installed": p.exists(), "kind": "systemd", "path": str(p),
                "detail": "systemd --user · Restart=on-failure"}
    if plat == "windows":
        ok = sm._run(["schtasks", "/Query", "/TN", WIN_TASK_NAME]).returncode == 0
        return {"installed": ok, "kind": "schtasks", "path": WIN_TASK_NAME,
                "detail": "计划任务 · 登录时启动"}
    return {"installed": False, "kind": "unknown", "path": "", "detail": ""}


def status_snapshot() -> dict:
    running, pid = supervisor_running()
    st = _read_state()
    healthy = sm.probe_port(CLIENT_PORT)
    csvc = st.get("client", {})
    return {
        "platform": sm.current_platform(),
        "supervisor": {"running": running, "pid": pid,
                       "started_at": st.get("started_at"), "ts": st.get("ts")},
        "autostart": autostart_status(),
        "client": {"port": CLIENT_PORT, "healthy": healthy,
                   "pid": csvc.get("pid"), "restarts": csvc.get("restarts", 0),
                   "last_restart": csvc.get("last_restart"), "managed": bool(csvc)},
        "now": time.time(),
    }


# ---- 安装 / 卸载(平台分支)----

def _mac_plist_xml() -> str:
    env = sm._env_for_service()
    env_xml = "".join(f"      <key>{k}</key><string>{v}</string>\n" for k, v in env.items())
    SUP_LOG.parent.mkdir(parents=True, exist_ok=True)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{sm._py_exe()}</string>
    <string>{CLIENT_SUP_PY}</string>
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


def _systemd_unit_text() -> str:
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in sm._env_for_service().items())
    return f"""[Unit]
Description=Clawworker Client Supervisor (client :{CLIENT_PORT} watchdog)
After=network.target

[Service]
Type=simple
WorkingDirectory={PROJECT_DIR}
{env_lines}
ExecStart={sm._py_exe()} {CLIENT_SUP_PY}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
"""


def install_autostart() -> str:
    plat = sm.current_platform()
    if plat == "darwin":
        p = _launchd_plist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_mac_plist_xml(), encoding="utf-8")
        sm._run(["launchctl", "unload", str(p)])
        r = sm._run(["launchctl", "load", "-w", str(p)])
        if r.returncode != 0:
            raise RuntimeError(f"launchctl load 失败: {r.stderr.strip() or r.stdout.strip()}")
        return f"已写入 {p.name} 并 launchctl load(登录自启 + 崩溃自愈)"
    if plat == "linux":
        p = _systemd_unit_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_systemd_unit_text(), encoding="utf-8")
        sm._run(["systemctl", "--user", "daemon-reload"])
        r = sm._run(["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.service"])
        if r.returncode != 0:
            raise RuntimeError(f"systemctl enable 失败: {r.stderr.strip() or r.stdout.strip()}")
        sm._run(["loginctl", "enable-linger", os.environ.get("USER", "")])
        return f"已写入 {p.name} 并 systemctl --user enable --now"
    if plat == "windows":
        tr = f'"{sm._py_exe()}" "{CLIENT_SUP_PY}"'
        r = sm._run(["schtasks", "/Create", "/F", "/SC", "ONLOGON",
                     "/TN", WIN_TASK_NAME, "/TR", tr, "/RL", "LIMITED"])
        if r.returncode != 0:
            raise RuntimeError(f"schtasks /Create 失败: {r.stderr.strip() or r.stdout.strip()}")
        ensure_supervisor_running()
        return f"已注册计划任务「{WIN_TASK_NAME}」(登录时启动客户端守护)"
    raise RuntimeError(f"不支持的平台: {plat}")


def uninstall_autostart() -> str:
    plat = sm.current_platform()
    if plat == "darwin":
        p = _launchd_plist_path()
        if p.exists():
            sm._run(["launchctl", "unload", "-w", str(p)])
            p.unlink()
            return f"已 launchctl unload 并删除 {p.name}"
        return "未安装(无 plist)"
    if plat == "linux":
        p = _systemd_unit_path()
        if p.exists():
            sm._run(["systemctl", "--user", "disable", "--now", f"{SYSTEMD_UNIT}.service"])
            p.unlink()
            sm._run(["systemctl", "--user", "daemon-reload"])
            return f"已 systemctl disable 并删除 {p.name}"
        return "未安装(无 unit)"
    if plat == "windows":
        r = sm._run(["schtasks", "/Delete", "/F", "/TN", WIN_TASK_NAME])
        if r.returncode != 0 and "ERROR" in (r.stderr + r.stdout).upper():
            return "未安装(无该计划任务)"
        return f"已删除计划任务「{WIN_TASK_NAME}」"
    raise RuntimeError(f"不支持的平台: {plat}")


def spawn_supervisor_detached() -> int:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    logf = open(SUP_LOG, "a", encoding="utf-8")  # noqa: SIM115
    kw: dict = {"cwd": str(PROJECT_DIR), "env": {**os.environ, **sm._env_for_service()},
                "stdout": logf, "stderr": logf}
    if sm.current_platform() == "windows":
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    return subprocess.Popen([sm._py_exe(), str(CLIENT_SUP_PY)], **kw).pid


def ensure_supervisor_running() -> bool:
    running, _ = supervisor_running()
    if running:
        return False
    spawn_supervisor_detached()
    return True


def stop_supervisor() -> str:
    running, pid = supervisor_running()
    if not running or not pid:
        return "客户端守护未在运行"
    try:
        if sm.current_platform() == "windows":
            sm._run(["taskkill", "/PID", str(pid), "/F"])
        else:
            os.kill(int(pid), 15)
    except OSError as e:
        return f"停止失败: {e}"
    return f"已向客户端守护(pid={pid})发送停止信号(子服务保留)"
