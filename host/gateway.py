"""
Clawworker 主机双入口。

同一个 FastAPI 应用、同一个 Python 进程同时监听:

- http://127.0.0.1:8442  本机管理入口
- https://0.0.0.0:8443   局域网客户端 / 远程管理入口

必须保持单进程:账户 session、Admin session、LLM provider 缓存和任务状态目前都在
内存里。若用两个独立 uvicorn 进程分别提供 HTTP/HTTPS,两个入口看到的运行态会分裂。

局域网 HTTPS 采用 fail-closed:证书生成或加载失败时整个主机服务启动失败,不允许
静默降级成明文 HTTP。客户端本机 UI(:8444)不经过本模块,明确使用 loopback HTTP。
"""
from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from pathlib import Path
from typing import Optional

import uvicorn

from host import tls_cert
from host.server import app


LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = int(os.environ.get("CLAWWORKER_HOST_LOCAL_PORT", "8442"))
LAN_HOST = os.environ.get("CLAWWORKER_HOST_LAN_BIND", "0.0.0.0")
LAN_PORT = int(os.environ.get("CLAWWORKER_HOST_LAN_PORT", "8443"))


def build_servers(cert_dir: Optional[Path] = None) -> tuple[uvicorn.Server, uvicorn.Server]:
    """
    构造本机 HTTP + 局域网 HTTPS 两个 uvicorn Server。

    两个 Server 共享同一个 ``host.server:app`` 对象。TLS server 负责应用 lifespan,
    HTTP server 关闭 lifespan,避免 startup/shutdown 事件执行两次。
    """
    cert_path, key_path, _fingerprint = tls_cert.ensure_cert(cert_dir or tls_cert.CERT_DIR)

    local_config = uvicorn.Config(
        app,
        host=LOCAL_HOST,
        port=LOCAL_PORT,
        timeout_keep_alive=75,
        lifespan="off",
        proxy_headers=False,
        log_level="info",
    )
    lan_config = uvicorn.Config(
        app,
        host=LAN_HOST,
        port=LAN_PORT,
        timeout_keep_alive=75,
        lifespan="on",
        proxy_headers=False,
        ssl_keyfile=str(key_path),
        ssl_certfile=str(cert_path),
        log_level="info",
    )
    return uvicorn.Server(local_config), uvicorn.Server(lan_config)


async def serve(cert_dir: Optional[Path] = None) -> None:
    local_server, lan_server = build_servers(cert_dir)
    servers = (local_server, lan_server)
    loop = asyncio.get_running_loop()

    def request_shutdown(*_args) -> None:
        for server in servers:
            server.should_exit = True

    # Server._serve() 不自行接管信号;由网关统一通知两个 listener,避免只有最后注册
    # signal handler 的那个 Server 退出。
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except (NotImplementedError, RuntimeError):
            with suppress(ValueError):
                signal.signal(sig, request_shutdown)

    try:
        # 调私有 _serve 是有意的:uvicorn.Server.serve() 会各自安装信号处理器,
        # 双 Server 同进程时后注册者会覆盖前者。版本由安装包固定为 0.48.0。
        await asyncio.gather(
            lan_server._serve(),  # noqa: SLF001
            local_server._serve(),  # noqa: SLF001
        )
    finally:
        request_shutdown()


def main() -> int:
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        # 让 supervisor 看见非零退出并记录明确原因;绝不回退 HTTP。
        print(f"[host-gateway] 启动失败:{type(exc).__name__}: {exc}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
