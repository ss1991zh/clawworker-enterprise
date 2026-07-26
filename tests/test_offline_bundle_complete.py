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
_WHEELS = _PKG / "wheels"


@pytest.mark.skipif(not _WHEELS.is_dir() or not any(_WHEELS.glob("*.whl")),
                    reason="本机无离线 wheel 包(不在版本库),跳过;打包机上会执行")
def test_offline_wheels_satisfy_all_requirements():
    """`pip install --dry-run --no-index` 必须能完整解析 requirements —— 缺件即失败。"""
    assert _REQ.is_file(), f"缺 {_REQ}"
    with tempfile.TemporaryDirectory() as td:
        # --dry-run 只解析不安装,但会走完整的依赖求解(含传递依赖),
        # 任何 wheel 缺失都会报 "No matching distribution"。
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--dry-run", "--no-index",
             "--find-links", str(_WHEELS), "-r", str(_REQ),
             "--target", td],
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
