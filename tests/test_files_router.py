from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from client.webui.routers.files import _owned_ciphertext, build_files_router


class _Storage:
    def __init__(self, root: Path) -> None:
        self.ciphertext_dir = root

    def list_ciphertexts(self):
        return list(self.ciphertext_dir.iterdir())


def _client(root: Path) -> TestClient:
    app = FastAPI()
    app.include_router(build_files_router(
        storage=_Storage(root),
        is_logged_in=lambda: True,
        need_login=lambda: {"error": "login"},
    ))
    return TestClient(app)


def test_files_router_lists_ciphertext_without_sidecars(tmp_path):
    root = tmp_path / "ciphertexts"
    root.mkdir()
    (root / "sales.xlsx").write_bytes(b"cipher")
    (root / "sales.xlsx.meta.csv").write_text("region\n", encoding="utf-8")
    (root / "sales.xlsx.schema.json").write_text("{}", encoding="utf-8")

    response = _client(root).get("/api/files")

    assert response.status_code == 200
    assert [item["name"] for item in response.json()] == ["sales.xlsx"]
    assert response.json()[0]["has_meta"] is True


def test_files_router_delete_removes_cipher_and_sidecars(tmp_path):
    root = tmp_path / "ciphertexts"
    root.mkdir()
    targets = [
        root / "sales.xlsx",
        root / "sales.xlsx.meta.csv",
        root / "sales.xlsx.schema.json",
    ]
    for target in targets:
        target.write_bytes(b"x")

    response = _client(root).delete("/api/files/sales.xlsx")

    assert response.status_code == 200
    assert not any(target.exists() for target in targets)


def test_owned_ciphertext_resolves_before_directory_boundary_check(tmp_path):
    root = tmp_path / "ciphertexts"
    root.mkdir()
    outside = tmp_path / "secret.xlsx"
    outside.write_bytes(b"secret")

    try:
        _owned_ciphertext(_Storage(root), "../secret.xlsx")
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 404
    else:
        raise AssertionError("path traversal must be rejected")
