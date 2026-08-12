from __future__ import annotations

import os

import pytest

from host import secret_store


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI only")
def test_dpapi_round_trip_and_ciphertext_does_not_contain_plaintext(tmp_path, monkeypatch):
    entropy_file = tmp_path / ".secret_entropy"
    monkeypatch.setattr(secret_store, "_ENTROPY_FILE", str(entropy_file))
    monkeypatch.setattr(secret_store, "_entropy_cache", None)

    plaintext = "database-password-不会明文落盘"
    protected = secret_store.protect(plaintext)

    assert secret_store.is_protected(protected)
    assert plaintext not in protected
    assert secret_store.unprotect(protected) == plaintext
