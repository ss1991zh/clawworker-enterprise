"""service_manager 单元测试 —— 不触碰真实开机自启,不 spawn 真服务。"""
import os
import socket
import subprocess
import time

import pytest

from host import service_manager as sm


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """把 state 路径指到临时目录,避免污染真实 supervisor 状态。"""
    monkeypatch.setattr(sm, "STATE_DIR", tmp_path)
    monkeypatch.setattr(sm, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(sm, "DESIRED_FILE", tmp_path / "desired_services.json")
    monkeypatch.setattr(sm, "SUP_LOG", tmp_path / "supervisor.log")
    return tmp_path


def test_platform_is_known():
    assert sm.current_platform() in ("darwin", "windows", "linux")


def test_service_run_argv_uses_gateway_for_host_and_http_for_client():
    argv = sm.service_run_argv("host")
    assert argv[-2:] == ["-m", "host.gateway"]

    argv_c = sm.service_run_argv("client")
    assert "uvicorn" in argv_c
    assert "client.webui:app" in argv_c
    assert "8444" in argv_c
    assert "--ssl-keyfile" not in argv_c
    assert "--ssl-certfile" not in argv_c


def test_host_health_requires_both_listeners(monkeypatch):
    open_ports = {8442}
    tls_ports = {8443}
    monkeypatch.setattr(
        sm,
        "probe_port",
        lambda port, host="127.0.0.1", timeout=0.6: port in open_ports,
    )
    monkeypatch.setattr(
        sm,
        "probe_tls_port",
        lambda port, host="127.0.0.1", timeout=0.6: port in tls_ports,
    )
    assert sm.service_healthy("host") is True

    tls_ports.remove(8443)
    assert sm.service_healthy("host") is False
    assert sm.service_healthy("client") is False


def test_probe_port_open_and_closed():
    # 开一个临时监听端口 → 探测应为 True
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert sm.probe_port(port) is True
    finally:
        srv.close()
    # 关闭后(同一端口大概率没人监听)→ False
    assert sm.probe_port(port) is False


def test_pid_alive():
    assert sm.pid_alive(os.getpid()) is True
    assert sm.pid_alive(0) is False
    assert sm.pid_alive(None) is False
    # 一个几乎不可能存在的 pid
    assert sm.pid_alive(2_000_000_000) is False


@pytest.mark.skipif(sm.current_platform() != "windows", reason="Windows 进程句柄语义")
def test_pid_alive_checks_exit_code_even_when_handle_is_still_open():
    proc = subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-Command", "Start-Sleep -Seconds 30"],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert sm.pid_alive(proc.pid) is True
        proc.terminate()
        proc.wait(timeout=5)
        # Popen 对象仍持有 Windows 进程句柄；旧实现会因此误判为仍存活。
        assert sm.pid_alive(proc.pid) is False
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_state_roundtrip(isolated_state):
    assert sm.read_state() == {}            # 无文件 → 空
    sm.write_state({"pid": 123, "ts": 1.0})
    assert sm.read_state()["pid"] == 123


def test_supervisor_running_logic(isolated_state):
    # 无状态 → 未运行
    running, _ = sm.supervisor_running()
    assert running is False

    # 自己的 pid + 新鲜心跳 → 运行中
    sm.write_state({
        "protocol": sm.SUPERVISOR_PROTOCOL,
        "pid": os.getpid(),
        "ts": time.time(),
        "services": {},
    })
    running, pid = sm.supervisor_running()
    assert running is True and pid == os.getpid()

    # 旧版即使 PID/心跳都有效,默认也不能冒充当前守护。
    sm.write_state({"pid": os.getpid(), "ts": time.time(), "services": {}})
    running, pid = sm.supervisor_running()
    assert running is False and pid == os.getpid()
    raw_running, _ = sm.supervisor_running(require_compatible=False)
    assert raw_running is True

    # 心跳过期 → 视为不健康
    sm.write_state({
        "protocol": sm.SUPERVISOR_PROTOCOL,
        "pid": os.getpid(),
        "ts": time.time() - 999,
        "services": {},
    })
    running, _ = sm.supervisor_running()
    assert running is False


def test_status_snapshot_shape(isolated_state, monkeypatch):
    monkeypatch.delenv("CLAWWORKER_MANAGED_SERVICES", raising=False)
    snap = sm.status_snapshot()
    assert set(snap) >= {"platform", "supervisor", "autostart", "services"}
    keys = {s["key"] for s in snap["services"]}
    assert keys == {"host"}        # 默认本机只托管控制面 host(client 在终端机)
    for s in snap["services"]:
        assert "healthy" in s and "port" in s and "label" in s
        if s["key"] == "host":
            assert s["local_port"] == 8442
            assert s["lan_port"] == 8443


def test_managed_service_keys_default_is_host(monkeypatch):
    monkeypatch.delenv("CLAWWORKER_MANAGED_SERVICES", raising=False)
    assert sm.managed_service_keys() == ["host"]


def test_managed_service_keys_env_override(monkeypatch):
    monkeypatch.setenv("CLAWWORKER_MANAGED_SERVICES", "client")
    assert sm.managed_service_keys() == ["client"]
    monkeypatch.setenv("CLAWWORKER_MANAGED_SERVICES", "host,client")
    assert sm.managed_service_keys() == ["host", "client"]
    # 非法值被过滤,回退默认
    monkeypatch.setenv("CLAWWORKER_MANAGED_SERVICES", "bogus")
    assert sm.managed_service_keys() == ["host"]


def test_autostart_status_shape():
    a = sm.autostart_status()
    assert set(a) >= {"installed", "kind", "path"}
    assert isinstance(a["installed"], bool)


def test_mac_plist_xml_wellformed():
    """plist 内容应含关键键(与平台无关,直接生成校验)。"""
    xml = sm._mac_plist_xml()
    assert "<key>RunAtLoad</key><true/>" in xml
    # KeepAlive 只在失败退出时重启(干净退出不 thrash)
    assert "<key>KeepAlive</key>" in xml
    assert "<key>SuccessfulExit</key><false/>" in xml
    assert sm.LAUNCHD_LABEL in xml
    assert "supervisor.py" in xml
    # XML 可被解析
    import xml.dom.minidom as minidom
    minidom.parseString(xml)


def test_systemd_unit_text_wellformed():
    txt = sm._systemd_unit_text()
    assert "Restart=on-failure" in txt
    assert "supervisor.py" in txt
    assert "[Install]" in txt


def test_restart_service_requires_supervisor(isolated_state):
    # 无守护时托管重启应报错
    with pytest.raises(RuntimeError, match="守护"):
        sm.restart_service("host")


def test_restart_service_unknown_key(isolated_state):
    with pytest.raises(RuntimeError, match="未知服务"):
        sm.restart_service("nope")
