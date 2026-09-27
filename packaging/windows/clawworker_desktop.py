"""Clawworker 用户端原生桌面窗口。

复用本机 http://127.0.0.1:8444 Web UI，仅将它承载在 Windows WebView2
窗口中。后台服务仍由 supervisor 管理，关闭窗口不会终止密态服务。
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
from pathlib import Path
from urllib.parse import urlparse
from typing import Callable, Optional


APP_TITLE = "Clawworker 用户端"
APP_USER_MODEL_ID = "Clawworker.Enterprise.Client"
MUTEX_NAME = r"Local\ClawworkerEnterpriseClientDesktop"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9


class SystemMeter:
    """Whole-machine CPU interval utilization and physical memory load (Windows)."""

    def __init__(self):
        self._previous = None
        self._lock = threading.Lock()

    @staticmethod
    def cpu_percent(previous, current):
        if previous is None:
            return None
        idle, kernel, user = (b - a for a, b in zip(previous, current))
        total = kernel + user  # Windows kernel time includes idle time.
        return max(0.0, min(100.0, 100 * (total - idle) / total)) if total > 0 else None

    def sample(self):
        if os.name != "nt":
            return {"cpu": None, "memory": None}
        from ctypes import wintypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("load", wintypes.DWORD)] + [
                (name, ctypes.c_ulonglong) for name in
                ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")
            ]
        with self._lock:
            values = [wintypes.FILETIME() for _ in range(3)]
            cpu = None
            if ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(v) for v in values)):
                current = tuple((v.dwHighDateTime << 32) | v.dwLowDateTime for v in values)
                cpu = self.cpu_percent(self._previous, current)
                self._previous = current
            memory = MemoryStatus()
            memory.length = ctypes.sizeof(memory)
            ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory))
            return {"cpu": cpu, "memory": float(memory.load) if ok else None}


class DesktopCompanion:
    """Small, non-sensitive native bridge; no file paths or shell access exposed."""

    def __init__(self, state_dir):
        self._path = state_dir / "desktop-companion.json"
        self._window = None
        self._main = None
        self._meter = SystemMeter()
        self._enabled = True
        self._authenticated = False
        self._lock = threading.Lock()
        try:
            self._enabled = json.loads(self._path.read_text(encoding="utf-8")).get("enabled", True) is not False
        except (OSError, ValueError, AttributeError):
            pass

    def companion_state(self):
        return {"available": self._window is not None, "enabled": self._enabled}

    def _sync_login(self):
        """Visibility follows the authenticated main page; preference is unchanged."""
        if self._main is None or self._window is None:
            return
        current = urlparse(self._main.get_current_url() or "")
        authenticated = current.hostname in {"127.0.0.1", "localhost"} and current.path == "/"
        with self._lock:
            self._authenticated = authenticated
            if authenticated and self._enabled:
                self._meter = SystemMeter()
                self._window.show()
            else:
                self._window.hide()

    def _disable_autofill(self):
        # Disable the native WebView saved-info popup as well as HTML autocomplete.
        if self._main is None or os.name != "nt":
            return
        from System import Action
        native = self._main.native
        def configure():
            settings = native.browser.webview.CoreWebView2.Settings
            settings.IsGeneralAutofillEnabled = False
            settings.IsPasswordAutosaveEnabled = False
        native.Invoke(Action(configure))

    def set_companion(self, enabled):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        with self._lock:
            if self._window is None:
                return {"available": False, "enabled": False}
            # Persist only an explicitly selected preference.
            self._path.parent.mkdir(parents=True, exist_ok=True)
            pending = self._path.with_suffix(".tmp")
            pending.write_text(json.dumps({"enabled": enabled}), encoding="utf-8")
            pending.replace(self._path)
            if enabled and self._authenticated:
                self._meter = SystemMeter()
                self._window.show()
            else:
                self._window.hide()
            self._enabled = enabled
            if self._main:
                self._main.evaluate_js("window.dispatchEvent(new Event('companionchange'))")
            return self.companion_state()

    def companion_metrics(self):
        if not self._enabled or not self._authenticated:
            return {"enabled": False, "cpu": None, "memory": None}
        return {"enabled": True, **self._meter.sample()}


def _set_windows_app_id() -> None:
    """让任务栏把窗口识别为独立的 Clawworker 应用。"""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def _claim_single_instance() -> tuple[Optional[int], bool]:
    """返回 (mutex 句柄, 是否为本次会话的首个窗口)。"""
    if os.name != "nt":
        return None, True
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        # 不能创建 mutex 时宁可继续打开，避免客户端完全不可用。
        return None, True
    return handle, kernel32.GetLastError() != ERROR_ALREADY_EXISTS


def _release_single_instance(handle: Optional[int]) -> None:
    if os.name == "nt" and handle:
        try:
            ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            pass


def _activate_existing_window() -> bool:
    """第二次双击图标时恢复并聚焦已有窗口。"""
    if os.name != "nt":
        return False

    user32 = ctypes.windll.user32
    found = {"value": False}
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    @callback_type
    def visit(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        title = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, title, length + 1)
        if title.value == APP_TITLE:
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            found["value"] = True
            return False
        return True

    try:
        user32.EnumWindows(visit, 0)
    except Exception:
        return False
    return found["value"]


def open_client_window(
    url: str,
    *,
    icon_path: Path,
    state_dir: Path,
    log: Callable[[str], None],
) -> bool:
    """打开用户端桌面窗口；成功或已激活现有窗口时返回 True。"""
    mutex, first_instance = _claim_single_instance()
    if not first_instance:
        activated = _activate_existing_window()
        log("检测到已打开的用户端窗口" + ("，已切换到前台" if activated else ""))
        _release_single_instance(mutex)
        return True

    try:
        import webview

        _set_windows_app_id()
        state_dir.mkdir(parents=True, exist_ok=True)
        storage_path = state_dir / "desktop-webview"

        # 下载 Excel/密文必须可用；外部链接仍交给默认浏览器，不允许 file:// 页面。
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["ALLOW_FILE_URLS"] = False
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        webview.settings["IGNORE_SSL_ERRORS"] = False
        webview.settings["REMOTE_DEBUGGING_PORT"] = None

        companion = DesktopCompanion(state_dir)
        main_window = webview.create_window(
            APP_TITLE,
            url=url,
            width=1180,
            height=760,
            min_size=(960, 640),
            resizable=True,
            maximized=False,
            background_color="#F7F8FA",
            text_select=True,
            zoomable=True,
            js_api=companion,
        )
        try:
            if main_window is not None:
                companion._main = main_window
                screen = webview.screens[0]
                companion._window = webview.create_window(
                    "Clawworker 桌面助手",
                    html=Path(__file__).with_name("desktop_companion.html").read_text(encoding="utf-8"),
                    js_api=companion, width=220, height=204, min_size=(220, 204),
                    x=max(0, screen.width - 250), y=max(0, screen.height - 274),
                    frameless=True, transparent=True, on_top=True, easy_drag=False,
                    resizable=False, focus=False, hidden=True,
                )
                def main_loaded():
                    try:
                        companion._disable_autofill()
                    except Exception as exc:
                        log(f"关闭自动填充失败 · {type(exc).__name__}: {exc}")
                    companion._sync_login()
                main_window.events.loaded += main_loaded
                main_window.events.closed += lambda: companion._window.destroy()
        except Exception as exc:
            companion._window = None
            log(f"桌面助手不可用，主窗口不受影响 · {type(exc).__name__}: {exc}")
        log(f"打开桌面窗口 · {url}")
        webview.start(
            gui="edgechromium",
            debug=False,
            private_mode=False,
            storage_path=str(storage_path),
            icon=str(icon_path),
        )
        return True
    except Exception as exc:
        log(f"桌面窗口启动失败 · {type(exc).__name__}: {exc}")
        return False
    finally:
        _release_single_instance(mutex)
