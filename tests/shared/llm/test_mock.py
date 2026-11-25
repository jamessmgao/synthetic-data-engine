"""The mock model (load tests): schema-valid answers, no spend."""

import json

from shared.llm import mock


def test_instances_follow_strict_schemas():
    schema = {"type": "object", "required": ["verdict", "items", "score", "ref"],
              "properties": {"verdict": {"type": "string", "enum": ["clean", "leak"]},
                             "items": {"type": "array", "items": {"type": "string"}},
                             "score": {"type": "number"}, "ref": {"$ref": "#/$defs/R"}},
              "$defs": {"R": {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}}}
    assert mock.instance(schema) == {"verdict": "clean", "items": [], "score": 0, "ref": {"ok": False}}


def test_registered_answers_win(monkeypatch):
    monkeypatch.setattr(mock, "LLM_S", 0)
    fmt = {"json_schema": {"name": "leak_review", "schema": {"type": "object"}}}
    out = mock.complete([], fmt)
    assert json.loads(out.content) == {"leaks": [], "verdict": "clean"} and out.cost_usd == 0.0


def test_llm_complete_uses_the_mock_when_enabled(monkeypatch):
    from shared.llm import llm

    monkeypatch.setattr(mock, "ENABLED", True)
    monkeypatch.setattr(mock, "LLM_S", 0)
    out = llm.complete([{"role": "user", "content": "x"}],
                       {"json_schema": {"name": "pass_review", "schema": {"type": "object", "required": ["a"],
                                                                          "properties": {"a": {"type": "string"}}}}})
    assert json.loads(out.content) == {"a": "mock"} and out.model == "mock"


def test_mock_answers_pass_the_leak_gate():
    """Load tests measure the clean path: no mock answer may read as a leak."""
    from shared.taskformat import leak

    assert json.loads(mock.content_for(leak.LEAK_FORMAT))["verdict"] == "clean"
    assert json.loads(mock.content_for(leak.JUDGE_FORMAT))["match"] != "specific"
