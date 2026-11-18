"""
A run at the agent time limit gets no automatic verdict (timeouts don't count as the
model failing). It settles as TIMEOUT; the Trace Parser rules on it from its trace: the model's
own failure → VALID_FAIL, anything else → INVALID, retried as before.

The traces are two real agent runs, condensed (evaluation/testdata/timeouts/):
- wedged_heredoc: aws-agent-b1-t09-v4-rollout2-r0. A 26 KB heredoc at step 5 left bash at its
  continuation prompt; 201 more steps over 56 minutes never got the shell back.
- progressing: aws-agent-b1-t03-v1-rollout3-r0. Still finding and fixing a real DP bug (a new
  counterexample at step 40) when time ran out.
"""

import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation import trace_parser
from evaluation.executor import analyze_job
from evaluation.trace_parser_service import TraceParserService
from shared.storage.sql import rollout_job_id
from api.submit import create_task
from tests.shared.conftest import backend, env  # noqa: F401 (fixtures: the backend matrix)
from tests.shared.jobs.test_services import T, analyze_all, build, claim, ids, review, run, settle, task

TESTDATA = Path(__file__).resolve().parents[1] / "evaluation" / "testdata" / "timeouts"


def fixture(name: str) -> dict:
    return json.loads(gzip.decompress((TESTDATA / f"{name}.json.gz").read_bytes()))


def write_trial(trial: Path, name: str) -> dict:
    fx = fixture(name)
    (trial / "agent").mkdir(parents=True, exist_ok=True)
    (trial / "verifier").mkdir(parents=True, exist_ok=True)
    (trial / "agent" / "trajectory.json").write_text(json.dumps(fx["trajectory"]))
    (trial / "verifier" / "test-stdout.txt").write_text(
        "FAILED tests/test_outputs.py::test_reconcile_within_limits - AssertionError: no report\n")
    result = {"trial_name": trial.name, **fx["result"]}
    (trial / "result.json").write_text(json.dumps(result))
    return result


def judge(calls: list | None = None, fail: bool = False, cite: bool = True):
    """A stand-in for GLM-5.1: rules from the signals line (E1), as the real prompt asks; the
    ordinary failure review for anything else."""
    other = review()

    def complete(messages, response_format, llm_dir=None, cache_key=""):
        if response_format["json_schema"]["name"] != "timeout_review":
            return other(messages, response_format, llm_dir, cache_key)
        if calls is not None:
            calls.append(cache_key)
        if fail:
            raise RuntimeError("gateway 502")
        line = next(ln for ln in messages[0]["content"].splitlines() if ln.startswith("[E1]"))
        signals = json.loads(line.split(") ", 1)[1])
        stuck = signals["unchanged_screen_minutes"] > 30
        reply = {"reason": "the shell is stuck at a continuation prompt" if stuck else "it was still debugging",
                 "category": "wedged_terminal" if stuck else "progressing",
                 "explanation": "E1 shows the screen unchanged [E1]", "cited_evidence": ["E1"] if cite else []}
        return SimpleNamespace(content=json.dumps(reply), cost_usd=0.003, cached=False)
    return complete


# --- the review itself, on the real traces ------------------------------------------------


@pytest.mark.parametrize("name", ["wedged_heredoc", "progressing"])
def test_signals_from_real_timed_out_runs(tmp_path, name):
    trial = tmp_path / "trial"
    result = write_trial(trial, name)
    s = trace_parser.timeout_signals(trial, result)
    if name == "wedged_heredoc":
        # From the oversized heredoc (step 5) on, the screen never changed again.
        assert s["agent_steps"] == 205 and s["unchanged_screen_steps"] == 201
        assert s["unchanged_screen_since_step"] == 6 and s["unchanged_screen_minutes"] > 55
        assert (s["largest_command_bytes"], s["largest_command_step"]) == (25973, 5)
    else:
        assert s["agent_steps"] == 39 and s["unchanged_screen_steps"] <= 3 and s["unchanged_screen_minutes"] < 2
    evidence = trace_parser.timeout_evidence(trial, s)
    assert evidence[0]["source"] == "signals" and json.loads(evidence[0]["text"]) == s
    # the steps where the screen stopped changing, then the last steps
    assert any(e["text"].startswith(f"step {s['unchanged_screen_since_step']} ") for e in evidence)
    assert evidence[-1]["text"].startswith("step 206 " if name == "wedged_heredoc" else "step 40 ")


def test_the_review_rules_model_or_not_and_fails_safe(tmp_path):
    wedged, progressing = tmp_path / "w", tmp_path / "p"
    rw, rp = write_trial(wedged, "wedged_heredoc"), write_trial(progressing, "progressing")
    kw = {"cache_key": "k", "llm_dir": None}
    w, cost = trace_parser.review_timeout(wedged, rw, complete=judge(), **kw)
    assert (w["verdict"], w["category"], w["cited_evidence"]) == ("model", "wedged_terminal", ["E1"]) and cost == 0.003
    p, _ = trace_parser.review_timeout(progressing, rp, complete=judge(), **kw)
    assert (p["verdict"], p["category"]) == ("not_model", "progressing")
    # No automatic verdict without a trusted review: a failed call or one citing nothing → not_model.
    failed, _ = trace_parser.review_timeout(wedged, rw, complete=judge(fail=True), **kw)
    assert failed["verdict"] == "not_model" and failed["review_error"].startswith("RuntimeError")
    uncited, _ = trace_parser.review_timeout(wedged, rw, complete=judge(cite=False), **kw)
    assert uncited["verdict"] == "not_model" and uncited["review_error"] == "the review cited no evidence"


# --- the Trace Parser service: ruling, then the decision ---------------------------------


def timed_out(backend, rid, name):
    """The executor settles a run that hit the agent time limit: TIMEOUT, no retry, its ping."""
    job = backend.store.job(rid)
    result = write_trial(backend.artifacts.trace_dir(rid) / f"{rid}__trial", name)
    settle(backend, rid, {"outcome": "TIMEOUT", "reward": None, "cost_usd": 0.5,
                          "error": "AgentTimeoutError: Agent execution timed out after 3600.0 seconds",
                          "result": {"trial": f"{rid}__trial", "raw_reward": 0.0}},
           followups=[analyze_job(job)])
    return result


def three_passes_and_a_fail(backend, parser):
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    for slot, mark in ((1, "1"), (2, "1"), (3, "1"), (4, "0")):  # 3 passes, 1 left: not certain yet
        run(backend, rollout_job_id(T, 1, slot, 0), mark)
    analyze_all(backend, parser)
    assert task(backend)["stage"] == "EVALUATING"


def test_a_wedged_terminal_is_ruled_a_model_failure_and_scored(backend, tmp_path):
    parser = TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=judge())
    three_passes_and_a_fail(backend, parser)
    timed_out(backend, rollout_job_id(T, 1, 5, 0), "wedged_heredoc")
    analyze_all(backend, parser)
    row = backend.store.job(rollout_job_id(T, 1, 5, 0))
    assert (row["outcome"], row["reward"]) == ("VALID_FAIL", 0.0)
    f = row["failure"]
    assert f["attribution"] == "model" and f["timeout_review"]["verdict"] == "model"
    assert f["timeout_review"]["category"] == "wedged_terminal" and f["conclusion"].startswith("the shell is stuck")
    assert f["failing_tests"] == ["tests/test_outputs.py::test_reconcile_within_limits"]
    # 3 of 5: in band, decided on the ruled run, never retried.
    t = task(backend)
    assert (t["status"], t["accepted_version"]) == ("ACCEPTED", 1)
    assert rollout_job_id(T, 1, 5, 1) not in ids(backend, type="rollout")
    decision = next(backend.store.job(a)["result"] for a in ids(backend, type="analyze")
                    if (backend.store.job(a)["result"] or {}).get("action") == "accept")
    assert decision["timeouts_ruled"] == {rollout_job_id(T, 1, 5, 0): "VALID_FAIL"}


def test_a_run_still_progressing_is_ruled_invalid_and_retried(backend, tmp_path):
    calls = []
    parser = TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=judge(calls))
    three_passes_and_a_fail(backend, parser)
    timed_out(backend, rollout_job_id(T, 1, 5, 0), "progressing")
    analyze_all(backend, parser)
    row = backend.store.job(rollout_job_id(T, 1, 5, 0))
    assert row["outcome"] == "INVALID" and row["reward"] is None
    assert row["error"].startswith("AgentTimeoutError") and "timeout review: progressing: it was still debugging" \
        in row["error"]
    assert row["failure"]["timeout_review"]["verdict"] == "not_model"
    assert task(backend)["stage"] == "EVALUATING"  # waits for the retry, as for any invalid run
    retry = rollout_job_id(T, 1, 5, 1)
    assert backend.store.job(retry)["status"] == "READY"
    run(backend, retry, "1")  # the retry passes: 4 of 5, too easy
    analyze_all(backend, parser)
    assert task(backend)["current_version"] == 2
    assert calls == [f"timeout-review:{rollout_job_id(T, 1, 5, 0)}"]  # ruled once


def test_an_unruled_timeout_is_remaining_never_auto_retried(backend, tmp_path):
    # A copy that can't reach its trace (none on disk) rules not_model: INVALID → the normal retry.
    parser = TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=judge())
    three_passes_and_a_fail(backend, parser)
    rid = rollout_job_id(T, 1, 5, 0)
    job = backend.store.job(rid)
    settle(backend, rid, {"outcome": "TIMEOUT", "reward": None, "error": "AgentTimeoutError: timed out"},
           followups=[analyze_job(job)])
    # Before any analysis: no retry exists, and the slot isn't scored.
    assert rollout_job_id(T, 1, 5, 1) not in ids(backend, type="rollout")
    analyze_all(backend, parser)
    row = backend.store.job(rid)
    assert row["outcome"] == "INVALID" and row["failure"]["timeout_review"]["review_error"] == "trace not found"
    assert backend.store.job(rollout_job_id(T, 1, 5, 1))["status"] == "READY"


def test_two_analyses_rule_once(backend, tmp_path):
    """Two copies analyze the same timed-out run: the first ruling wins, the second records stale."""
    parser = TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=judge())
    three_passes_and_a_fail(backend, parser)
    rid = rollout_job_id(T, 1, 5, 0)
    timed_out(backend, rid, "wedged_heredoc")
    ping = f"{rid}-analyze"
    late = claim(backend, ping)
    fields, handoff = parser.analyze(late)  # copy B rules, but settles last
    with backend.store.tx() as c:  # copy A rules and settles first
        c.execute("UPDATE jobs SET outcome = 'INVALID', error = 'ruled by A' WHERE id = ?", (rid,))
    with backend.store.tx() as c:
        assert handoff(c, {"id": ping}) == []
    assert backend.store.job(rid)["error"] == "ruled by A"
    assert backend.store.job(ping)["result"]["action"] == "stale"
    assert task(backend)["stage"] == "EVALUATING"
