"""The leak review's per-call timeouts, its concurrency, and inconclusive answers
(local-agent-b2: t04's review took ~14 min on one stalled call; an unparseable judge
silently passed). Scripted model calls, no spend."""

import json
import sys
import threading
import time
import types
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "generator")]
from validator import gates  # noqa: E402
from shared.llm import llm  # noqa: E402
from test_leak_review import P1_INSTRUCTION, bundle  # noqa: E402

CLEAN = {"verdict": "clean", "leaks": []}
PREDICTION = {"predicted_fix": "unclear", "predicted_failure": "unclear"}
NO_MATCH = {"match": "none", "why": "-"}


@pytest.fixture(autouse=True)
def review_on(monkeypatch):
    monkeypatch.setattr(gates.config, "LEAK_REVIEW", True)
    monkeypatch.setattr(gates.config, "LEAK_BLIND", True)
    monkeypatch.setattr(gates.config, "LEAK_INCONCLUSIVE", "warn")


class Scripted:
    """llm.complete stand-in: per call kind, outcomes consumed in order ("timeout", raw
    text, or a dict to JSON-encode; the last repeats); each call takes `delay` seconds."""

    def __init__(self, script: dict, delay: float = 0.0):
        self.script = {k: list(v) for k, v in script.items()}
        self.delay, self.starts, self.keys, self.lock = delay, {}, [], threading.Lock()

    def __call__(self, messages, response_format, llm_dir=None, cache_key=None, timeout=None, timeout_retries=0):
        kind = cache_key.removesuffix("-retry1")
        with self.lock:
            self.starts.setdefault(kind, time.monotonic())
            self.keys.append(cache_key)
            outcome = self.script[kind].pop(0) if len(self.script[kind]) > 1 else self.script[kind][0]
        time.sleep(self.delay)
        if outcome == "timeout":
            raise llm.CallTimeout(f"no complete answer within {timeout}s")
        content = outcome if isinstance(outcome, str) else json.dumps(outcome)
        return llm.Completion(content=content, finish_reason="stop", model="fake", prompt_tokens=1,
                              completion_tokens=1, reasoning_tokens=0, gateway_cost_usd=None, cost_usd=0.01,
                              duration_sec=self.delay, transport_retries=0, request_sha256=f"sha-{cache_key}")


def run(tmp_path, monkeypatch, model):
    monkeypatch.setattr(llm, "complete", model)
    return gates.gate_leak(bundle(tmp_path, P1_INSTRUCTION), tmp_path / "trace")


# --- llm.complete's timeout ------------------------------------------------------------------


def _chunk(text, usage=None):
    delta = types.SimpleNamespace(content=text, model_extra={})
    return types.SimpleNamespace(model="m", choices=[types.SimpleNamespace(delta=delta, finish_reason="stop")],
                                 usage=usage)


def test_a_stream_still_going_past_its_timeout_is_abandoned_and_retried(monkeypatch):
    attempts = []

    class Stream:
        def __init__(self, slow):
            self.slow, self.closed = slow, False

        def __iter__(self):
            if self.slow:  # a dribble: data keeps coming, so only a total deadline stops it
                time.sleep(0.3)
                yield _chunk("{")
                yield _chunk('"late": true}')
            else:
                yield _chunk(json.dumps(CLEAN), types.SimpleNamespace(
                    prompt_tokens=5, completion_tokens=7, model_extra={}, completion_tokens_details=None))

        def close(self):
            self.closed = True

    def create(**kw):
        attempts.append(Stream(slow=not attempts))
        return attempts[-1]

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    monkeypatch.setattr(llm, "_client", lambda timeout=None: client)
    out = llm.complete([{"role": "user", "content": "x"}], {}, timeout=0.2, timeout_retries=2)
    assert json.loads(out.content) == CLEAN and len(attempts) == 2 and attempts[0].closed


def test_complete_gives_up_after_its_timeout_retries(monkeypatch):
    calls = []

    def stalled(messages, response_format, timeout=None):
        calls.append(timeout)
        raise llm.CallTimeout("stalled")

    monkeypatch.setattr(llm, "_stream", stalled)
    with pytest.raises(llm.CallTimeout):
        llm.complete([{"role": "user", "content": "x"}], {}, timeout=1, timeout_retries=2)
    assert calls == [1, 1, 1]  # the attempt and two retries


def test_the_gate_passes_its_timeout_to_every_call(tmp_path, monkeypatch):
    seen = []

    def complete(messages, fmt, llm_dir=None, cache_key=None, timeout=None, timeout_retries=0):
        seen.append((cache_key, timeout, timeout_retries))
        return Scripted({"leak-review": [CLEAN], "leak-blind": [PREDICTION], "leak-judge": [NO_MATCH]})(
            messages, fmt, llm_dir, cache_key)

    monkeypatch.setattr(gates.config, "LEAK_CALL_TIMEOUT_S", 180.0)
    run(tmp_path, monkeypatch, complete)
    assert sorted(seen) == [("leak-blind", 180.0, 2), ("leak-judge", 180.0, 2), ("leak-review", 180.0, 2)]


# --- concurrency -------------------------------------------------------------------------------


def test_the_review_and_the_blind_prediction_run_concurrently(tmp_path, monkeypatch):
    model = Scripted({"leak-review": [CLEAN], "leak-blind": [PREDICTION], "leak-judge": [NO_MATCH]}, delay=0.4)
    start = time.monotonic()
    gate = run(tmp_path, monkeypatch, model)
    elapsed = time.monotonic() - start
    assert gate.passed and abs(model.starts["leak-review"] - model.starts["leak-blind"]) < 0.2
    assert elapsed < 1.1  # review ‖ blind (0.4 s), then the judge (0.4 s): not 3 × 0.4
    assert [c["call"] for c in gate.detail["calls"]] == ["leak-review", "leak-blind", "leak-judge"]
    assert gate.detail["cost_usd"] == pytest.approx(0.03)


# --- inconclusive answers ----------------------------------------------------------------------


def test_a_call_that_keeps_timing_out_is_inconclusive_not_a_crash(tmp_path, monkeypatch):
    model = Scripted({"leak-review": [CLEAN], "leak-blind": ["timeout"], "leak-judge": [NO_MATCH]})
    gate = run(tmp_path, monkeypatch, model)
    assert gate.passed and gate.detail["inconclusive"] == ["blind"] and gate.detail["blind_inconclusive"]
    assert "leak-judge" not in model.keys  # no prediction, so no judgement
    assert any(c.get("error") for c in gate.detail["calls"])  # the timed-out calls are recorded


@pytest.mark.parametrize("mode,passes", [("warn", True), ("fail", False)])
def test_an_unparseable_judge_is_retried_once_then_inconclusive(tmp_path, monkeypatch, mode, passes):
    monkeypatch.setattr(gates.config, "LEAK_INCONCLUSIVE", mode)
    model = Scripted({"leak-review": [CLEAN], "leak-blind": [PREDICTION], "leak-judge": ["not json", "still not"]})
    gate = run(tmp_path, monkeypatch, model)
    assert model.keys.count("leak-judge") == 1 and model.keys.count("leak-judge-retry1") == 1
    assert gate.detail["blind_inconclusive"] and gate.detail["blind"]["inconclusive"]
    assert not gate.detail["blind_leak"] and gate.passed is passes
    if not passes:
        assert "inconclusive" in gate.feedback


def test_a_judge_that_parses_on_its_retry_counts(tmp_path, monkeypatch):
    model = Scripted({"leak-review": [CLEAN], "leak-blind": [PREDICTION],
                      "leak-judge": ["{broken", {"match": "specific", "why": "names it"}]})
    gate = run(tmp_path, monkeypatch, model)
    assert not gate.passed and gate.detail["blind_leak"] and gate.detail["inconclusive"] == []


def test_an_unparseable_review_is_inconclusive(tmp_path, monkeypatch):
    model = Scripted({"leak-review": ["garbage"], "leak-blind": [PREDICTION], "leak-judge": [NO_MATCH]})
    gate = run(tmp_path, monkeypatch, model)
    assert gate.passed and gate.detail["inconclusive"] == ["review"] and gate.detail["verdict"] == "inconclusive"
