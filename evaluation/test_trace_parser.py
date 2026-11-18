"""Tests for the trace parser, on a trial directory shaped like a real Harbor 0.26 terminus-2 run.

Run with: python -m pytest evaluation
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evaluation import trace_parser
from evaluation.classify import VALID_FAIL, VALID_PASS

RESULT = {
    "agent_result": {"n_input_tokens": 1200, "n_output_tokens": 300, "cost_usd": 0.004},
    "agent_execution": {"started_at": "2026-11-17T22:34:00Z", "finished_at": "2026-11-17T22:37:30Z"},
}


def step(n, keystrokes, output, message="Plan: next", tool="bash_command"):
    return {
        "step_id": n, "source": "agent", "message": message, "metrics": {"prompt_tokens": 100},
        "tool_calls": [{"tool_call_id": f"call_{n}", "function_name": tool,
                        "arguments": {"keystrokes": keystrokes} if keystrokes is not None else {}}],
        "observation": {"results": [{"source_call_id": f"call_{n}", "content": output}]},
    }


@pytest.fixture
def trial(tmp_path) -> Path:
    d = tmp_path / "trial__abc"
    (d / "agent").mkdir(parents=True)
    (d / "verifier").mkdir()
    (d / "agent" / "trajectory.json").write_text(json.dumps({"steps": [
        {"step_id": 1, "source": "user", "message": "the task"},
        step(2, "cat > /etc/nginx/sites-enabled/app <<'EOF'\nserver { listen 8080; }\nEOF\n", ""),
        step(3, "sed -i 's/80/8080/' /etc/nginx/nginx.conf && nginx -s reload\n", "reloaded"),
        step(4, "curl -s localhost:8080/hello.html | tee /tmp/out.txt\n", "404 Not Found"),
        step(5, None, "", message="All components are configured.", tool="mark_task_complete"),
    ]}))
    (d / "verifier" / "ctrf.json").write_text(json.dumps({"results": {"tests": [
        {"name": "test_outputs.py::test_hello_html_exists", "status": "failed",
         "message": "The test failed in the call phase due to an assertion error",
         "trace": "def test_hello():\n>       assert 'TEST PASSED' in out\nE       AssertionError: Web server returned HTTP 404"},
        {"name": "test_outputs.py::test_repo_exists", "status": "passed", "trace": ""},
    ]}}))
    return d


class FakeLLM:
    def __init__(self, reply: dict | Exception, cost=0.003, cached=False):
        self.reply, self.cost, self.cached = reply, cost, cached
        self.prompts: list[str] = []

    def __call__(self, messages, response_format, llm_dir=None, cache_key=""):
        self.prompts.append(messages[0]["content"])
        if isinstance(self.reply, Exception):
            raise self.reply
        return SimpleNamespace(content=json.dumps(self.reply), cost_usd=self.cost, cached=self.cached)


def parse(trial, outcome, llm, tmp_path, sha=None):
    return trace_parser.parse(trial, RESULT, outcome, job_id="t1-v1-rollout1-r0", work_dir=tmp_path,
                              bundle_sha256=sha, bundle_dir=None, complete=llm)


def test_metrics_come_from_the_trajectory_and_result(trial, tmp_path):
    m, failure, cost = parse(trial, VALID_PASS, FakeLLM({}), tmp_path)
    assert failure is None and cost == 0.0  # a passing rollout gets no review
    assert m["llm_calls"] == 4 and m["input_tokens"] == 1200 and m["output_tokens"] == 300
    assert m["duration_seconds"] == 210.0 and m["declared_complete"] is True
    assert m["tool_calls"][1]["input"].startswith("sed -i") and m["tool_calls"][2]["output"] == "404 Not Found"
    assert m["files_modified"] == ["/etc/nginx/nginx.conf", "/etc/nginx/sites-enabled/app"]  # /tmp is scratch


def test_evidence_lists_failing_tests_assertions_and_the_agents_last_moves(trial):
    tests, evidence = trace_parser.extract_evidence(trial)
    assert [t["name"] for t in tests] == ["test_outputs.py::test_hello_html_exists"]
    texts = [e["text"] for e in evidence]
    assert texts[0].startswith("FAILED test_outputs.py::test_hello_html_exists")
    assert "E       AssertionError: Web server returned HTTP 404" in texts
    assert any(t.startswith("$ curl -s localhost:8080/hello.html") and "404 Not Found" in t for t in texts)
    assert "agent declared the task complete" in texts
    assert texts[-1] == "final message: All components are configured."
    assert [e["id"] for e in evidence] == [f"E{i}" for i in range(1, len(evidence) + 1)]


def test_a_failure_is_reviewed_with_the_hidden_spec(trial, tmp_path):
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "abc.json").write_text(json.dumps({"draft": {"spec": {
        "root_cause": "nginx serves /var/www/old, not the deploy dir",
        "expected_agent_pitfalls": ["editing the default site instead of the deploy root"]}}}))
    llm = FakeLLM({"explanation": "The agent edited the wrong site file (E4) and never fixed the root (E2).",
                   "attribution": "model", "pitfall_matched": "editing the default site instead of the deploy root",
                   "cited_evidence": ["E2", "E4", "E99"]})
    _, failure, cost = parse(trial, VALID_FAIL, llm, tmp_path, sha="abc")
    assert "nginx serves /var/www/old" in llm.prompts[0]  # the reviewer saw the hidden spec
    assert failure["attribution"] == "model" and failure["cited_evidence"] == ["E2", "E4"]  # unknown id dropped
    assert failure["pitfall_matched"].startswith("editing the default site")
    assert failure["failing_tests"] == ["test_outputs.py::test_hello_html_exists"]
    assert failure["evidence"][0]["id"] == "E1" and cost == 0.003


def test_a_task_defect_is_reported_as_such(trial, tmp_path):
    llm = FakeLLM({"explanation": "The test checks port 9090, the instruction says 8080 (E2).",
                   "attribution": "task", "pitfall_matched": "none", "cited_evidence": ["E2"]})
    _, failure, _ = parse(trial, VALID_FAIL, llm, tmp_path)
    assert failure["attribution"] == "task" and failure["pitfall_matched"] is None


def test_an_explanation_citing_nothing_real_is_unexplained(trial, tmp_path):
    llm = FakeLLM({"explanation": "It just failed.", "attribution": "model", "pitfall_matched": "none",
                   "cited_evidence": ["E99"]})
    _, failure, _ = parse(trial, VALID_FAIL, llm, tmp_path)
    assert failure["attribution"] == "unexplained" and failure["explanation"] == "It just failed."


def test_a_review_that_cannot_run_still_records_the_evidence(trial, tmp_path):
    _, failure, cost = parse(trial, VALID_FAIL, FakeLLM(ConnectionError("gateway down")), tmp_path)
    assert failure["attribution"] == "unexplained" and failure["review_error"].startswith("ConnectionError")
    assert failure["failing_tests"] and failure["evidence"] and cost == 0.0


def test_an_aborted_review_is_retried(trial, tmp_path):
    reply = {"explanation": "x (E1)", "attribution": "model", "pitfall_matched": "none", "cited_evidence": ["E1"]}
    calls = []

    def flaky(messages, response_format, llm_dir=None, cache_key=""):
        calls.append(cache_key)
        if len(calls) < 3:
            raise RuntimeError("Model output became abnormal while generating a JSON response")
        return SimpleNamespace(content=json.dumps(reply), cost_usd=0.003, cached=False)

    _, failure, cost = parse(trial, VALID_FAIL, flaky, tmp_path)
    assert len(calls) == 3 and failure["attribution"] == "model" and cost == 0.003


def test_a_pitfall_must_be_one_of_the_specs(trial, tmp_path):
    """Seen live: GLM-5.1 answered pitfall_matched with a sentence instead of 'none'."""
    (tmp_path / "drafts").mkdir()
    (tmp_path / "drafts" / "abc.json").write_text(json.dumps({"draft": {"spec": {
        "expected_agent_pitfalls": ["Serializing with one worker"]}}}))
    for answer, expected in [("None of the listed pitfalls match exactly.", None), ("none", None),
                             ("'serializing with one worker'", "Serializing with one worker")]:
        llm = FakeLLM({"explanation": "x (E1)", "attribution": "model", "pitfall_matched": answer,
                       "cited_evidence": ["E1"]})
        assert parse(trial, VALID_FAIL, llm, tmp_path, sha="abc")[1]["pitfall_matched"] == expected


def test_python_and_formatter_writes_are_files():
    """Seen live: an agent edited Go source through a Python heredoc and gofmt -w."""
    cmds = ["python3 - <<'PY'\npath = Path('internal/queue/queue.go')\ntext = path.read_text()\n"
            "path.write_text(text.replace('a', 'b'))\nPY",
            "/usr/local/go/bin/gofmt -w internal/queue/worker.go && go build ./...",
            "python3 -c \"print(Path('notes.md').read_text())\""]  # a read, not a write
    assert trace_parser.files_modified(cmds) == ["internal/queue/queue.go", "internal/queue/worker.go"]


def test_heredoc_comparisons_scratch_and_backups_are_not_files():
    """aws-agent-b1-t02's files_modified was ['/tmp/bad.out', 'Type,', 'engine.py', 'evaluated',
    'parsed', 'set', 'source', …]: a `>` in heredoc code captured words, and /tmp scratch counted,
    so one_file_patch never fired on a one-file fix."""
    cmds = ["python3 - <<'PY'\nif depth > evaluated:\n    raise Type, 'x'\nwhile n >= parsed: pass\nPY",
            "cat > engine.py <<'EOF'\nclass Engine: pass\nEOF",
            "./minilang run prog.ml > /tmp/bad.out 2>&1; echo $? > /tmp/service-hashes",
            "cp engine.py engine.py.orig; cp /app/engine.py /tmp/engine.bak",
            "sed -i 's/a/b/' /app/parser.py"]
    assert trace_parser.files_modified(cmds) == ["engine.py", "parser.py"]


def test_package_installs_and_flags_are_not_files():
    cmds = ["apt-get install -y nginx openssh-server", "git config -g user.name x > /dev/null",
            "sudo cp hook.sh /git/server/hooks/post-receive", "install -m 755 a.sh /usr/local/bin/a"]
    assert trace_parser.files_modified(cmds) == ["/git/server/hooks/post-receive", "/usr/local/bin/a"]


# aws-iter1-t01 v1 run 3's stored explanation, whole: an unescaped " ended the string under the
# gateway's constrained decoding, so the revise prompt said "the task is at fault" with no reason.
CUT = ("VERDICT: task\n\n1. The instruction and the tests align — no spec/test contradiction. The failing "
       "tests are test_invariant_1_at_most_one_debit_per_key (E1) and test_counterexample_exact (E7). Their "
       "assertion lines check exactly what the instruction's Guarantees require: a first submission returns status ")


def test_the_conclusion_comes_first_and_a_cut_explanation_is_flagged(trial, tmp_path):
    assert list(trace_parser.REVIEW_SCHEMA["json_schema"]["schema"]["properties"])[0] == "conclusion"
    llm = FakeLLM({"conclusion": "The tests insert raw SQL into a 'transactions' table the instruction never "
                                 "names: a hidden schema requirement.",
                   "explanation": CUT, "attribution": "task", "pitfall_matched": "none", "cited_evidence": ["E1"]})
    failure = parse(trial, VALID_FAIL, llm, tmp_path)[1]
    assert failure["conclusion"].startswith("The tests insert raw SQL") and failure["explanation"] == CUT
    assert failure["explanation_cut"] is True
    assert trace_parser.is_cut(CUT) and trace_parser.is_cut("the key was id=")
    assert not trace_parser.is_cut("It returned status 'accepted' (E3).") and not trace_parser.is_cut("a lock")


def test_the_revise_evidence_keeps_the_reason_and_the_assertions():
    from evaluation.calibration import evidence
    from shared.contracts.models import Outcome

    failure = {"conclusion": "The tests read a 'transactions' table the instruction never names.",
               "explanation": CUT, "failing_tests": ["test_outputs.py::test_counterexample_exact"],
               "evidence": [{"id": "E2", "source": "verifier", "text": "E       sqlite3.OperationalError: no such "
                                                                       "table: transactions"},
                            {"id": "E3", "source": "agent", "text": "$ ls /app"}]}
    text = evidence([{"slot": 3, "outcome": Outcome.VALID_FAIL, "failure": failure}])
    assert "a 'transactions' table the instruction never names" in text
    assert "returns status  [the reviewer's explanation is cut off here]" in text
    assert "failing tests: test_outputs.py::test_counterexample_exact" in text
    assert "E       sqlite3.OperationalError: no such table: transactions" in text and "$ ls /app" not in text


def test_a_solver_run_summary_shows_what_it_read_and_edited_at_which_step(trial, monkeypatch):
    s = trace_parser.solver_run_summary("run 2", trial)
    assert s.startswith("# run 2: passed in ") and "### step 1" in s and "    $ " in s
    assert s.splitlines()[1].startswith("Read (first step): ") and s.splitlines()[2].startswith("Edited (first step): ")
    monkeypatch.setattr(trace_parser, "SOLVER_RUN_BYTES", 300)  # long runs are cut to the cap
    assert len(trace_parser.solver_run_summary("run 2", trial)) <= 300 + 60
    assert trace_parser.solver_run_summary("run 2", trial / "missing") is None


def test_a_cached_review_costs_nothing(trial, tmp_path):
    llm = FakeLLM({"explanation": "x (E1)", "attribution": "model", "pitfall_matched": "none",
                   "cited_evidence": ["E1"]}, cached=True)
    assert parse(trial, VALID_FAIL, llm, tmp_path)[2] == 0.0


def test_stdout_fallback_when_there_is_no_ctrf(trial):
    (trial / "verifier" / "ctrf.json").unlink()
    (trial / "verifier" / "test-stdout.txt").write_text(
        "E       AssertionError: Did not pass test\nFAILED ../tests/test_outputs.py::test_hello - AssertionError\n")
    tests = trace_parser.failing_tests(trial)
    assert tests[0]["name"] == "../tests/test_outputs.py::test_hello"
    assert tests[0]["assertions"] == ["E       AssertionError: Did not pass test"]


def test_a_missing_trajectory_is_noted_not_fatal(tmp_path):
    (tmp_path / "t" / "verifier").mkdir(parents=True)
    tests, evidence = trace_parser.extract_evidence(tmp_path / "t")
    assert tests == [] and evidence[-1]["text"] == "no agent trajectory was recorded"


# --- why was it easy? review of passing runs ---------------------------------------


def pass_reply(**over):
    reply = {"why_easy": "The instruction names the faulty check [E2].", "simple_strategy": "read report.py",
             "cues": [{"quote": "the check must compare", "where": "instruction"}],
             "fix_applied": "compare against raw totals",
             "suggestions": [{"technique": "nonlocal_cause", "change": "move the symptom to the exporter",
                              "defeats": "the symptom no longer points at report.py"},
                             {"technique": "made_up_technique", "change": "x", "defeats": "y"}],
             "cited_evidence": ["E2", "E99"]}
    return {**reply, **over}


def test_pass_evidence_records_speed_analysis_and_commands(trial):
    ev = trace_parser.pass_evidence([("run 1", trial)])
    texts = [e["text"] for e in ev]
    assert ev[0]["id"] == "E1" and ev[0]["source"] == "run 1"
    assert texts[0].startswith("passed in 4 agent steps") and "/etc/nginx/sites-enabled/app" in texts[0]
    assert any(t.startswith("agent's analysis at step 2") for t in texts)
    assert any(t.startswith("$ sed -i") for t in texts)


def test_pass_review_keeps_only_real_citations_and_known_techniques(trial):
    llm = FakeLLM(pass_reply())
    analysis, cost = trace_parser.review_passes(trace_parser.pass_evidence([("run 1", trial)]), "the task",
                                                {"root_cause": "rc"}, passed=5, runs=5, cache_key="k",
                                                llm_dir=None, complete=llm)
    assert cost == 0.003 and analysis["trusted"] and analysis["cited_evidence"] == ["E2"]
    assert [s["technique"] for s in analysis["suggestions"]] == ["nonlocal_cause"]
    assert "5 of 5 runs" in llm.prompts[0] and "locally_correct_globally_wrong" in llm.prompts[0]
    assert "rc" in llm.prompts[0]


def test_pass_review_flags_fields_cut_mid_sentence(trial):
    reply = {**pass_reply(), "fix_applied": 'The agents made submit return status '}
    analysis, _ = trace_parser.review_passes(trace_parser.pass_evidence([("run 1", trial)]), "the task", None,
                                             passed=5, runs=5, cache_key="k", llm_dir=None, complete=FakeLLM(reply))
    assert analysis["cut_fields"] == ["fix_applied"]


def test_pass_review_citing_nothing_is_untrusted(trial):
    analysis, _ = trace_parser.review_passes(trace_parser.pass_evidence([("run 1", trial)]), "", None,
                                             passed=4, runs=5, cache_key="k", llm_dir=None,
                                             complete=FakeLLM(pass_reply(cited_evidence=["E99"])))
    assert analysis["trusted"] is False


def test_pass_review_that_cannot_run_is_recorded_not_raised():
    analysis, cost = trace_parser.review_passes([], "", None, passed=5, runs=5, cache_key="k", llm_dir=None,
                                                complete=FakeLLM(RuntimeError("gateway down")))
    assert analysis["trusted"] is False and "gateway down" in analysis["review_error"] and cost == 0.0


def test_pass_review_with_a_malformed_reply_or_broken_prompt_does_not_raise(trial, monkeypatch):
    ev = trace_parser.pass_evidence([("run 1", trial)])
    analysis, _ = trace_parser.review_passes(ev, "", None, passed=5, runs=5, cache_key="k", llm_dir=None,
                                             complete=FakeLLM(["not", "an", "object"]))
    assert analysis["trusted"] is False and "review_error" in analysis
    monkeypatch.setattr(trace_parser, "_hardening_techniques", lambda: 1 / 0)
    analysis, cost = trace_parser.review_passes(ev, "", None, passed=5, runs=5, cache_key="k", llm_dir=None,
                                                complete=FakeLLM(pass_reply()))
    assert analysis["trusted"] is False and "ZeroDivisionError" in analysis["review_error"] and cost == 0.0
