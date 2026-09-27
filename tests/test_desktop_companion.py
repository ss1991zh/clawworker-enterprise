import importlib.util
import sys
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("companion_test", Path(__file__).parents[1] / "packaging/windows/clawworker_desktop.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_cpu_delta_includes_kernel_idle():
    assert module.SystemMeter.cpu_percent(None, (1, 2, 3)) is None
    assert module.SystemMeter.cpu_percent((100, 200, 100), (130, 240, 160)) == 70
    assert module.SystemMeter.cpu_percent((1, 2, 3), (1, 2, 3)) is None


def test_toggle_default_persistence_and_hidden_sampling(tmp_path):
    class Window:
        def show(self): self.visible = True
        def hide(self): self.visible = False
    companion = module.DesktopCompanion(tmp_path)
    assert companion.companion_state()["enabled"] is True
    companion._window = Window()
    assert companion.set_companion(False)["enabled"] is False
    assert companion._window.visible is False
    assert companion.companion_metrics() == {"enabled": False, "cpu": None, "memory": None}
    assert module.DesktopCompanion(tmp_path).companion_state()["enabled"] is False
    assert companion.set_companion(True)["enabled"] is True
    with pytest.raises(ValueError):
        companion.set_companion("false")


def test_missing_window_never_claims_enabled(tmp_path):
    companion = module.DesktopCompanion(tmp_path)
    assert companion.set_companion(True) == {"available": False, "enabled": False}


def test_native_companion_lifecycle(monkeypatch, tmp_path):
    from types import SimpleNamespace
    class Event:
        def __iadd__(self, callback): self.callback = callback; return self
    class Window:
        def __init__(self): self.events = SimpleNamespace(closed=Event(), loaded=Event()); self.destroyed = False
        def destroy(self): self.destroyed = True
    calls = []
    def create_window(title, **kwargs):
        window = Window()
        calls.append((title, kwargs, window))
        return window
    fake = SimpleNamespace(settings={}, screens=[SimpleNamespace(width=1920, height=1080)],
        create_window=create_window, start=lambda **kwargs: None)
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(module, "_claim_single_instance", lambda: (None, True))
    monkeypatch.setattr(module, "_set_windows_app_id", lambda: None)
    assert module.open_client_window("http://127.0.0.1:8444", icon_path=tmp_path / "icon", state_dir=tmp_path, log=lambda text: None)
    assert len(calls) == 2
    options = calls[1][1]
    assert options["on_top"] and options["frameless"] and options["transparent"]
    assert options["hidden"]
    assert "CPU" in options["html"] and "内存" in options["html"]
    calls[0][2].events.closed.callback()
    assert calls[1][2].destroyed


def test_login_controls_visibility_without_overwriting_preference(tmp_path):
    class Window:
        def show(self): self.visible = True
        def hide(self): self.visible = False
    class Main:
        url = "http://127.0.0.1:8444/login"
        def get_current_url(self): return self.url
        def evaluate_js(self, script): pass
    companion = module.DesktopCompanion(tmp_path)
    companion._main = Main()
    companion._window = Window()
    companion._sync_login()
    assert not companion._window.visible
    assert companion._enabled
    companion._main.url = "http://127.0.0.1:8444/"
    companion._sync_login()
    assert companion._window.visible
    companion.set_companion(False)
    companion._main.url = "http://127.0.0.1:8444/login"
    companion._sync_login()
    companion._main.url = "http://127.0.0.1:8444/"
    companion._sync_login()
    assert not companion._window.visible
    assert not module.DesktopCompanion(tmp_path)._enabled
    companion.set_companion(True)
    assert module.DesktopCompanion(tmp_path)._enabled
