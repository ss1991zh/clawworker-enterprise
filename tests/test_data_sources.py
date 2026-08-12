from __future__ import annotations

import sqlite3

import pytest

from host.data_sources import (
    CatalogColumn,
    CredentialProtectionError,
    DataSourceStore,
)
from host.db_connectors import ConnectorRegistry, ConnectionTestResult, safe_error


def _store(tmp_path) -> DataSourceStore:
    return DataSourceStore(
        tmp_path / "control.db",
        protect=lambda value: "dpapi:test:" + value[::-1],
        unprotect=lambda value: value.removeprefix("dpapi:test:")[::-1],
        harden=lambda path: True,
        require_encryption=True,
    )


def _create(store: DataSourceStore, **overrides):
    values = dict(
        name="ERP生产库", engine="mysql", host="db.internal", port=3306,
        database_name="erp", username="clawworker_ro", password="very-secret",
        ssl_mode="require", connect_timeout_seconds=8,
        query_timeout_seconds=60, max_rows=10000,
    )
    values.update(overrides)
    return store.create(**values)


def test_password_encrypted_at_rest_and_never_in_public_object(tmp_path):
    store = _store(tmp_path)
    source = _create(store)

    assert "password" not in source.__dict__
    assert store.get_password(source.id) == "very-secret"

    with sqlite3.connect(store.db_path) as conn:
        cipher = conn.execute("SELECT password_cipher FROM data_sources").fetchone()[0]
    assert cipher.startswith("dpapi:")
    assert "very-secret" not in cipher
    assert "very-secret" not in store.db_path.read_bytes().decode("latin1")


def test_refuses_plaintext_password_when_encryption_is_required(tmp_path):
    store = DataSourceStore(
        tmp_path / "control.db", protect=lambda value: value,
        unprotect=lambda value: value, require_encryption=True,
        harden=lambda path: True,
    )
    with pytest.raises(CredentialProtectionError):
        _create(store)
    assert store.list_all() == []


def test_crud_toggle_and_password_preserved_on_blank_update(tmp_path):
    store = _store(tmp_path)
    source = _create(store)

    updated = store.update(source.id, name="ERP正式库", host="10.0.0.8", password="")
    assert updated.name == "ERP正式库"
    assert updated.host == "10.0.0.8"
    assert store.get_password(source.id) == "very-secret"

    assert store.set_enabled(source.id, False).enabled is False
    assert store.set_enabled(source.id, True).enabled is True
    store.delete(source.id)
    assert store.get(source.id) is None


@pytest.mark.parametrize("field,value", [
    ("engine", "oracle"), ("port", 70000), ("max_rows", 0),
    ("connect_timeout_seconds", 0), ("query_timeout_seconds", 4000),
    ("max_result_bytes", 1024), ("max_concurrent_queries", 0),
    ("max_queries_per_minute", 0),
])
def test_validation_rejects_unsafe_values(tmp_path, field, value):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        _create(store, **{field: value})


def test_catalog_replace_is_atomic_and_scoped(tmp_path):
    store = _store(tmp_path)
    source = _create(store)
    columns = [
        CatalogColumn("erp", "inventory", "BASE TABLE", "sku", "varchar(40)", 1, False),
        CatalogColumn("erp", "inventory", "BASE TABLE", "stock", "decimal(18,2)", 2, True),
    ]
    assert store.replace_catalog(source.id, columns) == 2
    assert store.catalog_summary(source.id) == {"schemas": 1, "tables": 1, "columns": 2}

    store.replace_catalog(source.id, columns[:1])
    assert [c.column_name for c in store.list_catalog(source.id)] == ["sku"]


def test_connection_registry_does_not_leak_password_in_error(tmp_path):
    store = _store(tmp_path)
    source = _create(store)
    registry = ConnectorRegistry()

    class Broken:
        def test(self, source, password):
            raise RuntimeError(f"password={password}; connection refused")

    registry._connectors["mysql"] = Broken()
    result = registry.test(source, "very-secret")
    assert result.ok is False
    assert "very-secret" not in result.message
    assert "password=***" in result.message


def test_record_connection_test_status(tmp_path):
    store = _store(tmp_path)
    source = _create(store)
    store.record_test(source.id, True, "连接成功")
    refreshed = store.get(source.id)
    assert refreshed.last_test_status == "ok"
    assert refreshed.last_test_message == "连接成功"
    assert refreshed.last_test_at


def test_safe_error_redacts_common_connection_string_passwords():
    message = safe_error(RuntimeError("PWD=hunter2; Password=secret host down"), "hunter2")
    assert "hunter2" not in message
    assert "secret" not in message


def test_old_control_database_is_migrated_with_execution_limits(tmp_path):
    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE data_sources (
            id TEXT PRIMARY KEY, name TEXT, engine TEXT, host TEXT, port INTEGER,
            database_name TEXT, username TEXT, password_cipher TEXT, ssl_mode TEXT,
            ca_path TEXT, connect_timeout_seconds INTEGER, query_timeout_seconds INTEGER,
            max_rows INTEGER, enabled INTEGER, created_at TEXT, updated_at TEXT,
            last_test_status TEXT, last_test_message TEXT, last_test_at TEXT,
            catalog_synced_at TEXT)""")
    store = DataSourceStore(db, harden=lambda path: True, require_encryption=False)
    with sqlite3.connect(db) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(data_sources)")}
    assert {"max_result_bytes", "max_concurrent_queries", "max_queries_per_minute"} <= columns
