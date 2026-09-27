"""审计日志哈希链完整性测试。"""
from __future__ import annotations

import json

from client.he_ops import audit


def _reset(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "AUDIT_DIR", tmp_path)
    audit._last_hash.clear()


def test_chain_verifies_clean(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    for i in range(5):
        audit._append("alice", {"kind": "test", "i": i})
    r = audit.verify_chain("alice")
    assert r["ok"] and r["total"] == 5


def test_tamper_field_detected(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    for i in range(4):
        audit._append("bob", {"kind": "test", "i": i})
    p = audit._path("bob")
    lines = p.read_text(encoding="utf-8").splitlines()
    ev = json.loads(lines[1]); ev["i"] = 999            # 篡改第2行字段(不改hash)
    lines[1] = json.dumps(ev, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = audit.verify_chain("bob")
    assert not r["ok"] and r["broken_at"] == 2


def test_deleted_line_detected(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    for i in range(4):
        audit._append("carol", {"kind": "test", "i": i})
    p = audit._path("carol")
    lines = p.read_text(encoding="utf-8").splitlines()
    del lines[1]                                         # 删掉第2行 → prev_hash 断链
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = audit.verify_chain("carol")
    assert not r["ok"]


def test_cipher_computation_proof_is_bound_to_message_and_chain(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    audit.set_context("alice", "session-1", "message-1")
    audit.record_llm_exposure(
        {"columns": [{"name": "revenue", "type": "decimal", "encrypted": True}]},
        "计算收入",
    )
    proof = audit.record_cipher_computation(
        run_id="run-1",
        input_fingerprint="a" * 64,
        output_fingerprint="b" * 64,
        skill_calls=["finance"],
    )

    assert proof["verified"] is True
    assert proof["chain_verified"] is True
    assert proof["no_structured_plaintext_to_llm"] is True
    events = audit.read_events("alice")
    assert events[-1]["type"] == "cipher_compute"
    assert events[-1]["message"] == "message-1"
    assert audit.verify_chain("alice")["ok"] is True


def test_cached_computation_is_verified_as_no_model_call(tmp_path, monkeypatch):
    _reset(tmp_path, monkeypatch)
    audit.set_context("alice", "session-cache", "message-cache")
    audit.record_llm_bypass()
    proof = audit.record_cipher_computation(
        run_id="run-cache", input_fingerprint="a" * 64,
        skill_calls=["codegen"],
    )

    assert proof["verified"] is True
    assert proof["llm_exposure_count"] == 0
    assert proof["llm_bypass_count"] == 1
    assert proof["no_structured_plaintext_to_llm"] is True
