"""
定时任务代码固化缓存回归测试 —— 锁住"同一任务每次运行输出结构一致"的机制:
  · 首次成功后保存,签名相同 → 命中复用
  · 任务问题变 / 数据 schema(列)变 → 签名失配 → 不命中(触发重新生成)
  · 删除 → 不命中
"""
from __future__ import annotations

import pytest

from client.webui.pipeline import (
    _codegen_cache_delete,
    _codegen_cache_load,
    _codegen_cache_save,
    _codegen_cache_sig,
)
from client.webui import codegen_cache, pipeline

SCHEMA = {"columns": [{"name": "回款金额(元)", "encrypted": True},
                      {"name": "实际销售额(元)", "encrypted": True},
                      {"name": "销售大区", "encrypted": False}]}

KEY = "task_testcache0001"


@pytest.fixture(autouse=True)
def isolated_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(codegen_cache, "CACHE_DIR", tmp_path)


def teardown_function(_fn):
    _codegen_cache_delete(KEY)


def test_sig_stable_for_same_question_and_schema():
    assert _codegen_cache_sig("回款率", SCHEMA) == _codegen_cache_sig("回款率", SCHEMA)


def test_sig_changes_on_question_or_schema_change():
    s1 = _codegen_cache_sig("回款率", SCHEMA)
    assert _codegen_cache_sig("回款率排名", SCHEMA) != s1
    schema2 = {"columns": SCHEMA["columns"] + [{"name": "新列", "encrypted": True}]}
    assert _codegen_cache_sig("回款率", schema2) != s1


def test_save_then_load_roundtrip():
    sig = _codegen_cache_sig("回款率", SCHEMA)
    _codegen_cache_save(KEY, sig, "full = ct.decrypt_df(cdf)", "测试摘要")
    hit = _codegen_cache_load(KEY, sig)
    assert hit is not None
    assert hit["code"] == "full = ct.decrypt_df(cdf)" and hit["summary"] == "测试摘要"
    assert hit["lazy_waived"] is False


def test_lazy_waived_flag_roundtrip():
    sig = _codegen_cache_sig("回款率", SCHEMA)
    _codegen_cache_save(KEY, sig, "code", "s", lazy_waived=True)
    hit = _codegen_cache_load(KEY, sig)
    assert hit is not None and hit["lazy_waived"] is True


def test_load_misses_on_sig_mismatch():
    sig = _codegen_cache_sig("回款率", SCHEMA)
    _codegen_cache_save(KEY, sig, "code", "s")
    other = _codegen_cache_sig("换了问题", SCHEMA)
    assert _codegen_cache_load(KEY, other) is None


def test_delete_then_miss():
    sig = _codegen_cache_sig("回款率", SCHEMA)
    _codegen_cache_save(KEY, sig, "code", "s")
    _codegen_cache_delete(KEY)
    assert _codegen_cache_load(KEY, sig) is None


def test_cache_cleanup_removes_expired_and_excess_files(tmp_path, monkeypatch):
    monkeypatch.setattr(codegen_cache, "MAX_FILES", 2)
    monkeypatch.setattr(codegen_cache, "MAX_AGE_SECONDS", 100)
    for index in range(4):
        path = tmp_path / f"task-{index}.json"
        path.write_text("{}", encoding="utf-8")
        path.touch()
        # 用明确时间保证排序稳定；最旧的文件同时会命中过期条件。
        import os
        os.utime(path, (1000 + index, 1000 + index))

    removed = pipeline._codegen_cache_cleanup(now=1050)

    assert removed == 2
    assert sorted(path.name for path in tmp_path.glob("*.json")) == ["task-2.json", "task-3.json"]
