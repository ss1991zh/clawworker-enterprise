from types import SimpleNamespace

import pytest

from client.webui import codegen


def test_tool_timing_preserves_results_and_times_each_call(monkeypatch):
    clock = iter([10.0, 10.125, 20.0, 20.25])
    monkeypatch.setattr(codegen.time, "perf_counter", lambda: next(clock))
    samples = []
    tools = codegen._TimedTools(SimpleNamespace(sum=lambda x: x + 1), samples.append)
    assert tools.sum(3) == 4
    assert tools.sum(7) == 8
    assert samples == [0.125, 0.25]


def test_plain_count_is_not_he_timing():
    samples = []
    tools = codegen._TimedTools(SimpleNamespace(count=lambda keys: len(keys)), samples.append, groupby=True)
    assert tools.count([1, 2]) == 2
    assert samples == []


def test_failed_call_is_timed_without_hiding_exception(monkeypatch):
    clock = iter([1.0, 1.5])
    monkeypatch.setattr(codegen.time, "perf_counter", lambda: next(clock))
    def fail():
        raise ValueError("test")
    samples = []
    tools = codegen._TimedTools(SimpleNamespace(sum=fail), samples.append)
    with pytest.raises(ValueError, match="test"):
        tools.sum()
    assert samples == [0.5]
