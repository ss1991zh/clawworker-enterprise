"""离线 wheel 包完整性守门 —— 防"目标机装完缺依赖、程序起不来"。

真实事故:wheels/ 漏了 cryptography(+其依赖 cffi/pycparser),而安装用 --no-index
纯离线 + --quiet 静默 → 目标机装完看似成功,实则少了 cryptography → 生成不了 TLS 证书
→ uvicorn 拿到不存在的证书文件启动即崩 → 用户"点图标没反应"。

本测试用 pip 的**离线依赖解析**核对 packaging/windows/requirements.txt 能否被
packaging/windows/wheels/ 完全满足(含传递依赖)。wheels 不在版本库(体积大),
故本机没有 wheels 时自动跳过;做安装包的机器上必然有,能在打包前拦住漏件。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys
import tempfile

import pytest

_PKG = pathlib.Path(__file__).resolve().parents[1] / "packaging" / "windows"
_REQ = _PKG / "requirements.txt"
_DESKTOP_REQ = _PKG / "requirements-desktop.txt"
_WHEELS = _PKG / "wheels"


@pytest.mark.skipif(not _WHEELS.is_dir() or not any(_WHEELS.glob("*.whl")),
                    reason="本机无离线 wheel 包(不在版本库),跳过;打包机上会执行")
def test_offline_wheels_satisfy_all_requirements():
    """`pip install --dry-run --no-index` 必须能完整解析 requirements —— 缺件即失败。"""
    assert _REQ.is_file(), f"缺 {_REQ}"
    with tempfile.TemporaryDirectory() as td:
        # --dry-run 只解析不安装,但会走完整的依赖求解(含传递依赖),
        # 任何 wheel 缺失都会报 "No matching distribution"。
        # 安装包固定使用 Windows x64 / CPython 3.11。显式指定目标标签,使打包机即使
        # 正在运行 Python 3.12/3.13,也按目标机环境检查 cp311 wheels。
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--no-index",
             "--find-links", str(_WHEELS), "-r", str(_REQ),
             "--target", td,
             "--platform", "win_amd64", "--python-version", "3.11",
             "--implementation", "cp", "--abi", "cp311",
             "--only-binary=:all:"],
            capture_output=True, text=True, errors="replace",
        )
    if r.returncode != 0:
        # 把 pip 报的缺件行挑出来,便于直接看出漏了谁
        lines = [ln for ln in (r.stderr + r.stdout).splitlines()
                 if "No matching" in ln or "Could not find" in ln]
        pytest.fail("离线 wheel 包无法满足 requirements(目标机会缺依赖装不起来):\n  "
                    + "\n  ".join(lines or [r.stderr[-500:]]))


@pytest.mark.skipif(not _WHEELS.is_dir() or not any(_WHEELS.glob("*.whl")),
                    reason="本机无离线 wheel 包,跳过")
def test_cryptography_wheel_present():
    """cryptography 是生成 TLS 证书的硬依赖,单独锁一条(这次事故就是它漏了)。"""
    got = [w.name for w in _WHEELS.glob("cryptography-*.whl")]
    assert got, "wheels 包里没有 cryptography —— 目标机将无法生成 TLS 证书、服务起不来"


@pytest.mark.skipif(not _WHEELS.is_dir() or not any(_WHEELS.glob("*.whl")),
                    reason="本机无离线 wheel 包,跳过")
def test_database_gateway_wheels_present():
    """远程数据库入口不能在目标机因缺驱动或 SQL 解析器而变成空壳。"""
    normalized = {w.name.lower().replace("-", "_") for w in _WHEELS.glob("*.whl")}
    for prefix in ("pymysql_", "psycopg_", "psycopg_binary_", "pyodbc_", "sqlglot_"):
        assert any(name.startswith(prefix) for name in normalized), f"缺少数据库离线 wheel：{prefix}"


def test_sql_server_odbc_offline_installer_present_and_wired():
    installer = _PKG / "db_drivers" / "msodbcsql18-x64.msi"
    vc_runtime = _PKG / "db_drivers" / "vc_redist.x64.exe"
    setup = (_PKG / "clawworker-setup.iss").read_text(encoding="utf-8")
    install = (_PKG / "install.ps1").read_text(encoding="utf-8")
    assert "db_drivers\\msodbcsql18-x64.msi" in setup
    assert "db_drivers\\vc_redist.x64.exe" in setup
    assert 'PrivilegesRequired=admin' in setup
    assert 'PrivilegesRequired=lowest' in setup
    assert '"/install","/quiet","/norestart"' in install
    assert "IACCEPTMSODBCSQLLICENSETERMS=YES" in install
    assert "ADDLOCAL=ALL" in install
    assert "ODBC Driver 18 for SQL Server" in install
    if not installer.is_file() and not vc_runtime.is_file():
        pytest.skip("微软数据库离线运行库不进入版本库，仅在打包机校验")
    assert installer.is_file() and installer.stat().st_size > 5_000_000
    assert vc_runtime.is_file() and vc_runtime.stat().st_size > 20_000_000


@pytest.mark.skipif(
    not (_PKG / "python-3.11.9-amd64.exe").is_file(),
    reason="Python 离线安装器不进入版本库，仅在打包机校验",
)
def test_bundled_python_311_installer_present():
    """目标机无需预装 Python:完整安装包必须携带固定的 CPython 3.11 x64 安装器。"""
    runtime = _PKG / "python-3.11.9-amd64.exe"
    assert runtime.is_file() and runtime.stat().st_size > 20_000_000


@pytest.mark.skipif(not _WHEELS.is_dir() or not any(_WHEELS.glob("*.whl")),
                    reason="本机无离线 wheel 包,跳过")
def test_offline_wheels_satisfy_desktop_requirements():
    """用户端桌面窗口依赖也必须能在断网目标机完整安装。"""
    assert _DESKTOP_REQ.is_file()
    with tempfile.TemporaryDirectory() as td:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--no-index",
             "--find-links", str(_WHEELS), "-r", str(_DESKTOP_REQ),
             "--target", td,
             "--platform", "win_amd64", "--python-version", "3.11",
             "--implementation", "cp", "--abi", "cp311",
             "--only-binary=:all:"],
            capture_output=True, text=True, errors="replace",
        )
    assert r.returncode == 0, (r.stderr + r.stdout)[-1200:]


@pytest.mark.skipif(
    not (
        _PKG
        / "webview2"
        / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    ).is_file(),
    reason="WebView2 离线安装器不进入版本库，仅在打包机校验",
)
def test_webview2_offline_installer_present():
    """用户端必须在无网络、目标机未预装 WebView2 时仍能打开桌面窗口。"""
    runtime = (
        _PKG
        / "webview2"
        / "MicrosoftEdgeWebView2RuntimeInstallerX64.exe"
    )
    assert runtime.is_file() and runtime.stat().st_size > 150_000_000


def test_webview2_install_is_client_only_and_verified():
    setup = (_PKG / "clawworker-setup.iss").read_text(encoding="utf-8")
    install = (_PKG / "install.ps1").read_text(encoding="utf-8")

    assert '#if MyRole == "client"' in setup
    assert "MicrosoftEdgeWebView2RuntimeInstallerX64.exe" in setup
    assert "Clawworker 用户端（浏览器诊断）" in setup
    assert "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in install
    assert '"/silent","/install"' in install
    assert "gui.renderer == 'edgechromium'" in install
