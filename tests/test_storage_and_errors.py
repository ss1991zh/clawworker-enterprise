from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shared import storage
from shared.http_observability import error_code_for_status
from shared.errors import ErrorCategory, classify_exception


def test_atomic_json_replace_failure_preserves_previous_file(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    storage.atomic_write_json(target, {"version": 1})

    def fail_replace(_source, _target):
        raise OSError("simulated power loss")

    monkeypatch.setattr(storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="power loss"):
        storage.atomic_write_json(target, {"version": 2})

    assert target.read_text(encoding="utf-8").strip().endswith("1\n}")
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_json_supports_list_payload(tmp_path):
    target = tmp_path / "items.json"
    storage.atomic_write_json(target, [{"id": 1}, {"id": 2}])
    assert '"id": 2' in target.read_text(encoding="utf-8")


def test_stable_http_error_codes():
    assert error_code_for_status(401) == "auth_required"
    assert error_code_for_status(422) == "validation_failed"
    assert error_code_for_status(503) == "service_unavailable"


def test_exception_categories_are_stable_and_safe():
    assert classify_exception(ConnectionError("secret host")).category is ErrorCategory.NETWORK
    assert classify_exception(PermissionError("private path")).category is ErrorCategory.PERMISSION
    assert classify_exception(ValueError("raw cell value")).category is ErrorCategory.DATA
    internal = classify_exception(RuntimeError("database password=secret"))
    assert internal.category is ErrorCategory.INTERNAL
    assert "secret" not in internal.public_message


def test_client_responses_include_request_and_error_headers():
    from client.webui.app import app

    client = TestClient(app)
    ok = client.get("/healthz", headers={"host": "127.0.0.1", "x-request-id": "client-test-123"})
    assert ok.headers["x-clawworker-request-id"] == "client-test-123"

    missing = client.get("/missing", headers={"host": "127.0.0.1"})
    assert missing.headers["x-clawworker-error-code"] == "not_found"
    assert len(missing.headers["x-clawworker-request-id"]) >= 8


def test_host_responses_include_request_id():
    from host.server import app

    response = TestClient(app).get("/healthz", headers={"x-request-id": "host-test-123"})
    assert response.headers["x-clawworker-request-id"] == "host-test-123"
