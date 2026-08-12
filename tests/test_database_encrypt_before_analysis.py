from __future__ import annotations

import importlib
from pathlib import Path


app_mod = importlib.import_module("client.webui.app")
pipeline_mod = importlib.import_module("client.webui.pipeline")


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

    monkeypatch.setattr(app_mod.tempfile, "NamedTemporaryFile", NamedTemp)
    monkeypatch.setattr("pandas.DataFrame.to_excel", fake_to_excel)
    monkeypatch.setattr(app_mod, "_ingest_plaintext_path", fake_encrypt)

    response = app_mod._encrypt_database_result(
        {
            "request_id": "req-1", "columns": ["id", "amount"],
            "rows": [[1, 100], [2, 200]], "row_count": 2, "duration_ms": 8,
        },
        {"data_source_id": "erp"},
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
    monkeypatch.setattr(app_mod.tempfile, "NamedTemporaryFile", NamedTemp)
    monkeypatch.setattr("pandas.DataFrame.to_excel",
                        lambda self, path, index=False: Path(path).write_bytes(b"secret"))
    monkeypatch.setattr(
        app_mod, "_ingest_plaintext_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("encrypt failed")),
    )

    try:
        app_mod._encrypt_database_result(
            {"columns": ["id"], "rows": [[1]], "row_count": 1},
            {"data_source_id": "erp"},
        )
    except Exception as exc:
        assert "自动加密失败" in str(exc)
    else:
        raise AssertionError("加密失败时必须报错，不能返回明文")
    assert not plaintext.exists()


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

    monkeypatch.setattr(app_mod.tempfile, "NamedTemporaryFile", NamedTemp)
    monkeypatch.setattr("pandas.DataFrame.to_excel",
                        lambda self, path, index=False: Path(path).write_bytes(b"row secret"))
    monkeypatch.setattr(app_mod, "_ingest_plaintext_path", fake_encrypt)
    monkeypatch.setattr(pipeline_mod, "load_schema",
                        lambda path: {"columns": [{"name": "amount"}]})
    monkeypatch.setattr(pipeline_mod, "load_metadata", lambda path: ([], []))
    monkeypatch.setattr(pipeline_mod, "_run_codegen_path", fake_codegen)

    encrypted = app_mod._encrypt_database_result(
        {"columns": ["id", "amount"], "rows": [[1, 100]], "row_count": 1},
        {"data_source_id": "erp"},
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
