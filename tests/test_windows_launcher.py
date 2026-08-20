"""Windows 桌面启动器的 URL、就绪检查和失败反馈。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


_PATH = (
    Path(__file__).resolve().parents[1]
    / "packaging"
    / "windows"
    / "clawworker_launch.py"
)
_SPEC = importlib.util.spec_from_file_location("clawworker_launch_test", _PATH)
launcher = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(launcher)

_DESKTOP_PATH = _PATH.with_name("clawworker_desktop.py")
_DESKTOP_SPEC = importlib.util.spec_from_file_location(
    "clawworker_desktop_test", _DESKTOP_PATH
)
desktop = importlib.util.module_from_spec(_DESKTOP_SPEC)
assert _DESKTOP_SPEC and _DESKTOP_SPEC.loader
_DESKTOP_SPEC.loader.exec_module(desktop)


def test_role_urls_match_local_http_design():
    assert launcher.ROLES["admin"]["url"] == "http://127.0.0.1:8442/admin"
    assert launcher.ROLES["admin"]["ready_url"].endswith(":8442/readyz")
    assert launcher.ROLES["client"]["url"] == "http://127.0.0.1:8444"
    assert launcher.ROLES["client"]["ready_url"].endswith(":8444/readyz")
    assert "8443" not in launcher.ROLES["admin"]["url"]


def test_http_probe_directly_connects_without_system_proxy(monkeypatch):
    connections = []

    class FakeConnection:
        def __init__(self, host, port, timeout):
            connections.append((host, port, timeout, self))
            self.request_args = None
            self.closed = False

        def request(self, *args, **kwargs):
            self.request_args = (args, kwargs)

        def getresponse(self):
            return type("Response", (), {"status": 200})()

        def close(self):
            self.closed = True

    monkeypatch.setattr(launcher.http.client, "HTTPConnection", FakeConnection)

    assert launcher._http_ready("http://127.0.0.1:8444/readyz") is True
    host, port, timeout, connection = connections[0]
    assert (host, port, timeout) == (
        "127.0.0.1",
        8444,
        launcher.HTTP_PROBE_TIMEOUT_SEC,
    )
    assert connection.request_args[0] == ("GET", "/readyz")
    assert connection.closed is True
    assert launcher.HTTP_PROBE_TIMEOUT_SEC >= 2.0


def test_http_probe_rejects_non_loopback_url():
    assert launcher._http_ready("http://192.168.3.169:8444/healthz") is False


def test_http_probe_keeps_waiting_on_not_ready(monkeypatch):
    class FakeConnection:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return type("Response", (), {"status": 503})()

        def close(self):
            pass

    monkeypatch.setattr(launcher.http.client, "HTTPConnection", FakeConnection)
    assert launcher._http_ready("http://127.0.0.1:8444/readyz") is False


def test_main_waits_for_http_then_opens_browser(monkeypatch, tmp_path):
    checks = iter([False, True, True])
    started = []
    opened = []
    monkeypatch.setattr(sys, "argv", ["launcher", "admin"])
    monkeypatch.setattr(launcher, "_find_project_dir", lambda _start: tmp_path)
    monkeypatch.setattr(launcher, "_http_ready", lambda _url: next(checks))
    monkeypatch.setattr(launcher, "_port_up", lambda _port: False)
    monkeypatch.setattr(launcher, "_start_supervisor", lambda root, svc: started.append((root, svc)))
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(launcher, "_log", lambda _message: None)

    assert launcher.main() == 0
    assert started == [(tmp_path, "host")]
    assert opened == ["http://127.0.0.1:8442/admin"]


def test_main_does_not_restart_when_role_port_is_already_up(monkeypatch, tmp_path):
    checks = iter([False, True, True])
    started = []
    opened = []
    monkeypatch.setattr(sys, "argv", ["launcher", "admin"])
    monkeypatch.setattr(launcher, "_find_project_dir", lambda _start: tmp_path)
    monkeypatch.setattr(launcher, "_http_ready", lambda _url: next(checks))
    monkeypatch.setattr(launcher, "_port_up", lambda _port: True)
    monkeypatch.setattr(
        launcher,
        "_start_supervisor",
        lambda root, svc: started.append((root, svc)),
    )
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(launcher, "_log", lambda _message: None)

    assert launcher.main() == 0
    assert started == []
    assert opened == ["http://127.0.0.1:8442/admin"]


def test_client_error_message_points_to_client_log():
    message = launcher._error_message("用户端", "client")

    assert str(launcher.CLIENT_LOG) in message
    assert str(launcher.HOST_LOG) not in message


def test_client_opens_desktop_window_not_browser(monkeypatch, tmp_path):
    desktop_calls = []
    opened = []
    monkeypatch.setattr(sys, "argv", ["launcher", "client"])
    monkeypatch.setattr(launcher, "_find_project_dir", lambda _start: tmp_path)
    monkeypatch.setattr(launcher, "_http_ready", lambda _url: True)
    monkeypatch.setattr(
        launcher,
        "_open_client_desktop",
        lambda root, url: desktop_calls.append((root, url)) or True,
    )
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(launcher, "_log", lambda _message: None)

    assert launcher.main() == 0
    assert desktop_calls == [(tmp_path, "http://127.0.0.1:8444")]
    assert opened == []


def test_client_browser_diagnostic_switch(monkeypatch, tmp_path):
    opened = []
    monkeypatch.setattr(sys, "argv", ["launcher", "client", "--browser"])
    monkeypatch.setattr(launcher, "_find_project_dir", lambda _start: tmp_path)
    monkeypatch.setattr(launcher, "_http_ready", lambda _url: True)
    monkeypatch.setattr(
        launcher,
        "_open_client_desktop",
        lambda _root, _url: (_ for _ in ()).throw(AssertionError("不应打开桌面窗口")),
    )
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(launcher, "_log", lambda _message: None)

    assert launcher.main() == 0
    assert opened == ["http://127.0.0.1:8444"]


def test_main_does_not_open_dead_page(monkeypatch, tmp_path):
    errors = []
    opened = []
    monkeypatch.setattr(sys, "argv", ["launcher", "admin"])
    monkeypatch.setattr(launcher, "_find_project_dir", lambda _start: tmp_path)
    monkeypatch.setattr(launcher, "_http_ready", lambda _url: False)
    monkeypatch.setattr(launcher, "_port_up", lambda _port: False)
    monkeypatch.setattr(launcher, "_start_supervisor", lambda _root, _svc: None)
    monkeypatch.setattr(launcher, "STARTUP_TIMEOUT_SEC", 0.0)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launcher.webbrowser, "open", opened.append)
    monkeypatch.setattr(
        launcher, "_show_error", lambda label, _svc: errors.append(label)
    )
    monkeypatch.setattr(launcher, "_log", lambda _message: None)

    assert launcher.main() == 1
    assert opened == []
    assert errors == ["管理端"]


def test_startup_timeout_uses_wall_clock_deadline(monkeypatch, tmp_path):
    clock = [0.0]
    monkeypatch.setattr(sys, "argv", ["launcher", "admin"])
    monkeypatch.setattr(launcher, "_find_project_dir", lambda _start: tmp_path)
    monkeypatch.setattr(launcher, "_http_ready", lambda _url: False)
    monkeypatch.setattr(launcher, "_port_up", lambda _port: False)
    monkeypatch.setattr(launcher, "_start_supervisor", lambda _root, _svc: None)
    monkeypatch.setattr(launcher, "STARTUP_TIMEOUT_SEC", 1.0)
    monkeypatch.setattr(launcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        launcher.time,
        "sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + seconds),
    )
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(launcher, "_show_error", lambda _label, _svc: None)
    monkeypatch.setattr(launcher, "_log", lambda _message: None)

    assert launcher.main() == 1
    assert 1.0 <= clock[0] < 1.5


def test_desktop_window_enables_downloads_and_persistent_storage(
    monkeypatch, tmp_path
):
    class FakeWebView:
        settings = {}

        def __init__(self):
            self.window_kwargs = {}
            self.start_kwargs = {}

        def create_window(self, title, **kwargs):
            self.title = title
            self.window_kwargs = kwargs

        def start(self, **kwargs):
            self.start_kwargs = kwargs

    fake = FakeWebView()
    released = []
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(desktop, "_claim_single_instance", lambda: (123, True))
    monkeypatch.setattr(desktop, "_release_single_instance", released.append)
    monkeypatch.setattr(desktop, "_set_windows_app_id", lambda: None)

    ok = desktop.open_client_window(
        "http://127.0.0.1:8444",
        icon_path=tmp_path / "clawworker.ico",
        state_dir=tmp_path / "state",
        log=lambda _message: None,
    )

    assert ok is True
    assert fake.title == "Clawworker 用户端"
    assert fake.window_kwargs["url"] == "http://127.0.0.1:8444"
    assert fake.window_kwargs["maximized"] is True
    assert fake.settings["ALLOW_DOWNLOADS"] is True
    assert fake.settings["ALLOW_FILE_URLS"] is False
    assert fake.start_kwargs["gui"] == "edgechromium"
    assert fake.start_kwargs["debug"] is False
    assert fake.start_kwargs["private_mode"] is False
    assert fake.start_kwargs["storage_path"].endswith("desktop-webview")
    assert released == [123]


def test_second_client_click_activates_existing_window(monkeypatch):
    released = []
    monkeypatch.setattr(desktop, "_claim_single_instance", lambda: (456, False))
    monkeypatch.setattr(desktop, "_activate_existing_window", lambda: True)
    monkeypatch.setattr(desktop, "_release_single_instance", released.append)

    assert desktop.open_client_window(
        "http://127.0.0.1:8444",
        icon_path=Path("clawworker.ico"),
        state_dir=Path("state"),
        log=lambda _message: None,
    ) is True
    assert released == [456]
