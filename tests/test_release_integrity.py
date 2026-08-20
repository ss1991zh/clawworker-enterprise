"""发布阻塞项：文本披露边界、Anthropic 离线能力和桌面就绪检查。"""
from __future__ import annotations

import importlib
import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException

from client.he_ops import audit
from host import llm_proxy
from shared.version import __version__


def test_text_attachment_requires_explicit_llm_consent():
    app_mod = importlib.import_module("client.webui.app")
    with pytest.raises(HTTPException) as exc:
        app_mod._validated_text_attachments({
            "text_attachments": [{"name": "规则.docx", "content": "毛利=收入-成本"}],
        })
    assert exc.value.status_code == 400
    assert "明确同意" in str(exc.value.detail)


def test_text_attachment_consent_keeps_document_context_but_caps_size():
    app_mod = importlib.import_module("client.webui.app")
    result = app_mod._validated_text_attachments({
        "text_attachment_llm_consent": True,
        "text_attachments": [{"name": "规则.docx", "content": "x" * 40_000}],
    })
    assert result[0]["name"] == "规则.docx"
    assert len(result[0]["content"]) == 30_000


def test_document_exposure_audit_records_metadata_not_content(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    audit._last_hash.clear()
    secret_text = "毛利=收入-成本"
    audit.record_document_exposure(
        [{"name": "经营规则.docx", "content": secret_text}],
        user="alice",
        session_id="s1",
    )
    events = audit.read_events("alice")
    assert events[-1]["type"] == "document_exposure"
    assert events[-1]["explicit_consent"] is True
    assert events[-1]["documents"] == [{"name": "经营规则.docx", "chars": len(secret_text)}]
    assert secret_text not in (tmp_path / "alice.jsonl").read_text(encoding="utf-8")
    assert audit.summary("alice")["document_exposures"] == 1


def test_anthropic_provider_uses_existing_http_transport(monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "content": [{"type": "text", "text": "完成"}],
                "usage": {"input_tokens": 12, "output_tokens": 3},
                "stop_reason": "end_turn",
            }

    class FakeClient:
        def __init__(self, **kwargs):
            seen["init"] = kwargs

        def post(self, path, json):
            seen["path"] = path
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr(llm_proxy.httpx, "Client", FakeClient)
    provider = llm_proxy.AnthropicLLMProvider("key", model="claude-test")

    assert provider.raw_chat("system", "user") == "完成"
    assert seen["path"] == "/messages"
    assert seen["json"]["model"] == "claude-test"
    assert seen["init"]["trust_env"] is False
    assert provider.last_usage == {"prompt_tokens": 12, "completion_tokens": 3}


def test_anthropic_http_error_is_user_readable(monkeypatch):
    class FakeResponse:
        status_code = 401

        @staticmethod
        def json():
            return {"error": {"message": "invalid key"}}

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def post(self, _path, json):
            return FakeResponse()

    monkeypatch.setattr(llm_proxy.httpx, "Client", FakeClient)
    provider = llm_proxy.AnthropicLLMProvider("bad-key")
    with pytest.raises(RuntimeError, match="HTTP 401"):
        provider.raw_chat("system", "user")


def test_product_version_is_consistent_across_runtime_and_installer():
    root = Path(__file__).resolve().parents[1]
    assert __version__ == "1.7.0"
    assert importlib.import_module("host.server").app.version == __version__
    assert importlib.import_module("client.webui.app").app.version == __version__
    assert 'version = "1.7.0"' in (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '#define AppVersion "1.7.0"' in (
        root / "packaging" / "windows" / "clawworker-setup.iss"
    ).read_text(encoding="utf-8")


def test_post_install_smoke_targets_ready_endpoint(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "packaging" / "windows" / "post_install_smoke.py"
    spec = importlib.util.spec_from_file_location("post_install_smoke_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    seen = {}

    class FakeResponse:
        status = 200

        @staticmethod
        def read(_limit):
            return b'{"status":"ready"}'

    class FakeConnection:
        def __init__(self, host, port, timeout):
            seen["connection"] = (host, port, timeout)

        def request(self, method, target, headers):
            seen["request"] = (method, target, headers)

        def getresponse(self):
            return FakeResponse()

        def close(self):
            seen["closed"] = True

    monkeypatch.setattr(module.http.client, "HTTPConnection", FakeConnection)
    assert module._ready(18444)[0] is True
    assert seen["request"][1] == "/readyz"
    assert seen["closed"] is True


def test_windows_dependencies_are_split_by_role():
    root = Path(__file__).resolve().parents[1]
    packaging = root / "packaging" / "windows"
    common = (packaging / "requirements-common.txt").read_text(encoding="utf-8")
    admin = (packaging / "requirements-admin.txt").read_text(encoding="utf-8")
    client = (packaging / "requirements-client.txt").read_text(encoding="utf-8")
    combined = (packaging / "requirements.txt").read_text(encoding="utf-8")

    assert "fastapi" in common and "cryptography" in common
    assert "pip==" in common and "setuptools==" in common and "wheel==" in common
    assert "sqlglot" in admin and "pyodbc" in admin
    assert "pandas" not in admin and "xgboost" not in admin
    assert "pandas" in client and "xgboost" in client
    assert "pyodbc" not in client and "sqlglot" not in client
    assert "requirements-admin.txt" in combined
    assert "requirements-client.txt" in combined


def test_windows_installer_uses_role_specific_offline_payloads():
    root = Path(__file__).resolve().parents[1]
    packaging = root / "packaging" / "windows"
    installer = (packaging / "install.ps1").read_text(encoding="utf-8")
    builder = (packaging / "build_installers.ps1").read_text(encoding="utf-8")
    inno = (packaging / "clawworker-setup.iss").read_text(encoding="utf-8")

    assert '"requirements-$Role.txt"' in installer
    assert '$Role -eq "client" -or $Role -eq "both"' in installer
    assert "检测到旧虚拟环境不可用" in installer
    assert '"wheels-$role"' in builder
    assert "Get-FileHash" in builder and "SHA256SUMS-$AppVersion.txt" in builder
    assert 'Source: "wheels-admin\\*"' in inno
    assert 'Source: "wheels-client\\*"' in inno
    assert inno.index('#if MyRole == "client"\nSource: "he_libs\\*"') > 0
