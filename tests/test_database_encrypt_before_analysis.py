from __future__ import annotations

from pathlib import Path

import pytest

from client.webui import pipeline as pipeline_mod
from client.webui.services import database as database_service_mod
from client.webui.services.database import DatabaseAnalysisService, DatabaseResultEncryptionError


def test_database_rows_are_encrypted_before_response_and_plaintext_is_deleted(
    tmp_path, monkeypatch,
):
    seen = {}

    class NamedTemp:
        def __init__(self, *args, **kwargs):
            self.name = str(tmp_path / "database-plaintext.xlsx")

        def __enter__(self):
            Path(self.name).touch()
            return self

        def __exit__(self, *args):
            return False

    def fake_to_excel(self, path, index=False):
        assert index is False
        Path(path).write_bytes(b"plaintext database rows")

    def fake_encrypt(path, original_name, *, dst_stem=None):
        path = Path(path)
        assert path.exists()
        assert path.read_bytes() == b"plaintext database rows"
        seen["plaintext_path"] = path
        seen["original_name"] = original_name
        return {
            "name": "database-result.cipher",
            "path": str(tmp_path / "database-result.cipher"),
            "encrypted_columns": ["amount"],
            "plaintext_columns": ["id"],
            "row_count": 2,
        }

    monkeypatch.setattr("pandas.DataFrame.to_excel", fake_to_excel)
    service = DatabaseAnalysisService(lambda *a, **k: None, fake_encrypt,
                                      named_temporary_file=NamedTemp)
    response = service.encrypt_result(
        {
            "request_id": "req-1", "columns": ["id", "amount"],
            "rows": [[1, 100], [2, 200]], "row_count": 2, "duration_ms": 8,
        },
        data_source_id="erp",
    )

    assert seen["original_name"].startswith("数据库提取_erp_")
    assert not seen["plaintext_path"].exists()
    assert "rows" not in response
    assert response["encrypted"]["path"].endswith("database-result.cipher")
    assert response["row_count"] == 2


def test_encryption_failure_still_deletes_plaintext(tmp_path, monkeypatch):
    class NamedTemp:
        def __init__(self, *args, **kwargs):
            self.name = str(tmp_path / "failed-plaintext.xlsx")

        def __enter__(self):
            Path(self.name).touch()
            return self

        def __exit__(self, *args):
            return False

    plaintext = tmp_path / "failed-plaintext.xlsx"
    monkeypatch.setattr("pandas.DataFrame.to_excel",
                        lambda self, path, index=False: Path(path).write_bytes(b"secret"))
    service = DatabaseAnalysisService(
        lambda *a, **k: None,
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("encrypt failed")),
        named_temporary_file=NamedTemp,
    )
    with pytest.raises(DatabaseResultEncryptionError, match="encrypt failed"):
        service.encrypt_result(
            {"columns": ["id"], "rows": [[1]], "row_count": 1},
            data_source_id="erp",
        )
    assert not plaintext.exists()


def test_local_excel_size_limit_is_checked_before_encryption(tmp_path, monkeypatch):
    class NamedTemp:
        def __init__(self, *args, **kwargs):
            self.name = str(tmp_path / "oversized.xlsx")
        def __enter__(self):
            Path(self.name).touch()
            return self
        def __exit__(self, *args): return False

    encrypted_called = False
    def must_not_encrypt(*args, **kwargs):
        nonlocal encrypted_called
        encrypted_called = True

    monkeypatch.setattr("pandas.DataFrame.to_excel",
                        lambda self, path, index=False: Path(path).write_bytes(b"x" * 100))
    service = DatabaseAnalysisService(lambda *a, **k: None, must_not_encrypt,
                                      named_temporary_file=NamedTemp)
    with pytest.raises(DatabaseResultEncryptionError, match="超过管理员设置"):
        service.encrypt_result(
            {"columns": ["id"], "rows": [[1]], "max_result_bytes": 50},
            data_source_id="erp",
        )
    assert encrypted_called is False
    assert not (tmp_path / "oversized.xlsx").exists()


def test_model_analysis_starts_only_after_database_result_is_local_cipher(
    tmp_path, monkeypatch,
):
    events = []
    cipher_path = tmp_path / "database-result.cipher"

    class NamedTemp:
        def __init__(self, *args, **kwargs):
            self.name = str(tmp_path / "database-plaintext.xlsx")

        def __enter__(self):
            Path(self.name).touch()
            return self

        def __exit__(self, *args):
            return False

    def fake_encrypt(path, original_name, *, dst_stem=None):
        assert Path(path).exists()
        cipher_path.write_bytes(b"local cipher only")
        events.append(("encrypted", str(cipher_path)))
        return {
            "name": cipher_path.name, "path": str(cipher_path),
            "encrypted_columns": ["amount"], "plaintext_columns": ["id"],
            "row_count": 1,
        }

    def fake_codegen(**kwargs):
        assert kwargs["cipher_path"] == cipher_path
        assert cipher_path.read_bytes() == b"local cipher only"
        events.append(("model_analysis", str(kwargs["cipher_path"])))
        return {
            "status": "done", "summary": "ok", "excel_path": "",
            "skill_calls": ["codegen"], "error": "",
        }

    monkeypatch.setattr("pandas.DataFrame.to_excel",
                        lambda self, path, index=False: Path(path).write_bytes(b"row secret"))
    monkeypatch.setattr(pipeline_mod, "load_schema",
                        lambda path: {"columns": [{"name": "amount"}]})
    monkeypatch.setattr(pipeline_mod, "load_metadata", lambda path: ([], []))
    monkeypatch.setattr(pipeline_mod, "_run_codegen_path", fake_codegen)

    service = DatabaseAnalysisService(lambda *a, **k: None, fake_encrypt,
                                      named_temporary_file=NamedTemp)
    encrypted = service.encrypt_result(
        {"columns": ["id", "amount"], "rows": [[1, 100]], "row_count": 1},
        data_source_id="erp",
    )
    result = pipeline_mod._ask_impl(
        user_query="分析数据库查询结果并汇总金额",
        cipher_path=Path(encrypted["encrypted"]["path"]),
        host_url="http://127.0.0.1:8000", token="test",
        system_prompt="test",
    )

    assert result["status"] == "done", result
    assert [item[0] for item in events] == ["encrypted", "model_analysis"]
    assert not (tmp_path / "database-plaintext.xlsx").exists()


def test_conversation_database_flow_returns_only_cipher_metadata(tmp_path, monkeypatch):
    """会话一键查询链路不得把数据库明文或 SQL 带到后续分析参数。"""
    secret = "CUSTOMER-PLAINTEXT-MUST-NOT-REACH-MODEL"
    cipher_path = tmp_path / "conversation-db.cipher"
    calls = []
    statuses = iter([{"status": "running"}, {"status": "success"}])

    def fake_host_api(method, path, **kwargs):
        calls.append((method, path, kwargs.get("json_body")))
        if path == "/data/query/plan":
            # 规划模型只接收用户问题；真实数据行此时尚未查询。
            assert secret not in repr(kwargs)
            return {"sql": "SELECT customer_name, amount FROM sales_order"}
        if path == "/data/query/tasks":
            return {"task_id": "task-1", "status": "queued"}
        if path == "/data/query/tasks/task-1":
            return next(statuses)
        if path == "/data/query/tasks/task-1/result":
            return {
                "columns": ["customer_name", "amount"],
                "rows": [[secret, 99]], "row_count": 1,
            }
        raise AssertionError((method, path))

    def fake_encrypt(result, payload):
        assert result["rows"][0][0] == secret
        # 模拟本地加密完成后才产生密文文件。
        result.pop("rows")
        cipher_path.write_bytes(b"encrypted-only")
        return {
            "row_count": 1,
            "encrypted": {
                "path": str(cipher_path), "name": cipher_path.name,
                "encrypted_columns": ["amount"],
                "plaintext_columns": ["customer_name"],
            },
        }

    monkeypatch.setattr(database_service_mod.time, "sleep", lambda _: None)
    service = DatabaseAnalysisService(
        fake_host_api,
        lambda *a, **k: {},
        result_encryptor=lambda result, source_id: fake_encrypt(
            result, {"data_source_id": source_id},
        ),
    )
    safe = service.prepare_cipher(
        data_source_id="erp", intent="汇总销售金额",
        on_step=lambda *_: None, should_cancel=lambda: False,
    )

    assert safe["path"] == str(cipher_path)
    assert cipher_path.read_bytes() == b"encrypted-only"
    assert secret not in repr(safe)
    assert "SELECT" not in repr(safe)
    assert "rows" not in safe
