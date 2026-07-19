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
