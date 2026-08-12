"""Clawworker 用户端原生桌面窗口。

复用本机 http://127.0.0.1:8444 Web UI，仅将它承载在 Windows WebView2
窗口中。后台服务仍由 supervisor 管理，关闭窗口不会终止密态服务。
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Callable, Optional


APP_TITLE = "Clawworker 用户端"
APP_USER_MODEL_ID = "Clawworker.Enterprise.Client"
MUTEX_NAME = r"Local\ClawworkerEnterpriseClientDesktop"
ERROR_ALREADY_EXISTS = 183
SW_RESTORE = 9


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

        webview.create_window(
            APP_TITLE,
            url=url,
            width=1280,
            height=820,
            min_size=(960, 640),
            resizable=True,
            maximized=True,
            background_color="#F5F7FB",
            text_select=True,
            zoomable=True,
        )
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
