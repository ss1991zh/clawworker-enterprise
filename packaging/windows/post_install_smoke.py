"""安装后真实启动角色服务并等待 /readyz；失败时让安装程序明确中止。"""
from __future__ import annotations

import argparse
import http.client
import socket
import subprocess
import sys
import time
from pathlib import Path


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ready(port: int) -> tuple[bool, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2.0)
    try:
        connection.request("GET", "/readyz", headers={"Connection": "close"})
        response = connection.getresponse()
        body = response.read(4096).decode("utf-8", errors="replace")
        return response.status == 200, f"HTTP {response.status}: {body}"
    except OSError as exc:
        return False, type(exc).__name__
    finally:
        connection.close()


def run(role: str, project: Path, timeout: float = 60.0) -> None:
    target = "host.server:app" if role == "admin" else "client.webui:app"
    port = _free_loopback_port()
    state_dir = Path.home() / ".agent-system" / "supervisor"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / f"install-smoke-{role}.log"
    last_status = "服务尚未响应"
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", target,
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "info",
            ],
            cwd=str(project),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"{role} 冒烟服务提前退出（代码 {process.returncode}）")
                ok, last_status = _ready(port)
                if ok:
                    return
                time.sleep(0.25)
            raise RuntimeError(f"{role} 在 {int(timeout)} 秒内未就绪；最后状态：{last_status}")
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--role", choices=("admin", "client"), required=True)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    try:
        run(args.role, args.project.resolve(), args.timeout)
    except Exception as exc:  # noqa: BLE001 - 安装器需要单一、可读的失败出口
        log_path = Path.home() / ".agent-system" / "supervisor" / f"install-smoke-{args.role}.log"
        print(f"安装后启动自检失败：{exc}\n日志：{log_path}", file=sys.stderr)
        return 1
    print(f"{args.role} 安装后启动自检通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
