"""The services' hand-offs and the Trace Parser's decisions against a real SQLite
store (PRD §5): submission, Generation Service, Build + Validate, the executor's
settle and the Trace Parser. Workers are simulated by claiming a job and settling
it through the real JobQueue with the real hand-off, the way the service loops do;
only GLM-5.1 drafting, the gates, Harbor and the review are replaced."""

import json
import threading
from types import SimpleNamespace

import pytest

from evaluation.executor import analyze_job
from evaluation.trace_parser_service import TraceParserService
from shared.jobs import handoffs
from shared.storage import from_env
from shared.storage.sql import claim as sql_claim, insert_job, job_id, rollout_job_id
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import MAX_VERSIONS, JobType
from shared.jobs.reconciler import reconcile
from api.submit import BatchConflict, TaskConflict, create_batch, create_task
from shared.taxonomy.taxonomy import TAXONOMY, UNIQUE_DIMENSIONS, sample_spec

T = "t-1"


def review(attribution="model"):
    """A stand-in for GLM-5.1's failure review: blames `attribution`, citing E1."""
    def complete(messages, response_format, llm_dir=None, cache_key=""):
        first = next(line for line in messages[0]["content"].splitlines() if line.startswith("[E1]"))
        reply = {"explanation": f"{first.split(') ', 1)[1]} [E1]", "attribution": attribution,
                 "pitfall_matched": "none", "cited_evidence": ["E1"]}
        return SimpleNamespace(content=json.dumps(reply), cost_usd=0.004, cached=False)
    return complete


@pytest.fixture
def backend(tmp_path):
    return from_env({"SDE_WORK_DIR": str(tmp_path / "work")})


@pytest.fixture
def parser(backend, tmp_path):
    return TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=review())


def task(backend, id=T):
    return backend.store.task(id)


def ids(backend, id=T, type=None):
    return [j["id"] for j in backend.store.task_jobs(id) if type is None or j["type"] == type]


def claim(backend, jid):
    with backend.store.tx() as c:
        job = sql_claim(c, "host:1:w", [JobType(backend.store.job(jid)["type"])], only_id=jid)
    assert job, f"{jid} not claimable"
    return job


def settle(backend, jid, fields, handoff=None, on_start=None, followups=()):
    job = claim(backend, jid)
    if on_start:
        on_start(job)
    assert JobQueue(backend).settle(jid, job["lease_token"], fields, followups, then=handoff)


def generate(backend, jid, sha, name="queue-drain-bug", ok=True):
    """The Generation Service settles a draft (bundle written to the artifact store)."""
    if ok:
        bundle = backend.artifacts.bundle_path(sha)
        bundle.mkdir(parents=True, exist_ok=True)
        (bundle / "instruction.md").write_text(f"fix it ({sha})\n")
        fields = {"outcome": "OK", "bundle_sha256": sha, "result": {"name": name, "bundle_sha256": sha},
                  "cost_usd": 0.25}
    else:
        fields = {"outcome": "FAILED", "error": "no acceptable draft", "cost_usd": 0.25}
    settle(backend, jid, fields, handoffs.after_generation(backend), handoffs.generation_started(backend))


def validate(backend, jid, ok=True, feedback="oracle failed"):
    fields = {"outcome": "OK", "result": {"gates": {}}} if ok else \
        {"outcome": "FAILED", "result": {"feedback": feedback}, "error": feedback}
    settle(backend, jid, fields, handoffs.after_validate(backend), handoffs.validate_started(backend))


def run(backend, rid, mark):
    """The executor settles one run: trace on disk, outcome, its analyze ping, and
    (for an INVALID run) the retry of its slot."""
    job = backend.store.job(rid)
    trial = backend.artifacts.trace_dir(rid) / f"{rid}__trial"
    (trial / "verifier").mkdir(parents=True, exist_ok=True)
    stdout = "PASSED tests/test_outputs.py::test_happy_path\n"
    if mark == "0":
        stdout += f"FAILED tests/test_outputs.py::test_crash_recovery - AssertionError: lost job {job['slot']}\n"
    (trial / "verifier" / "test-stdout.txt").write_text(stdout)
    (trial / "result.json").write_text(json.dumps({"trial_name": trial.name, "finished_at": "2026-11-17T00:00:00Z",
                                                   "agent_result": {"cost_usd": 0.01}}))
    outcome = {"1": "VALID_PASS", "0": "VALID_FAIL", "i": "INVALID"}[mark]
    fields = {"outcome": outcome, "reward": None if mark == "i" else float(mark == "1"),
              "result": {"trial": trial.name}, "cost_usd": 0.01}
    followups = [analyze_job(job)]
    if mark == "i" and job["retry"] < 3:
        followups.append({"id": rollout_job_id(T, job["version"], job["slot"], job["retry"] + 1),
                          "type": JobType.ROLLOUT, "task_id": job["task_id"], "version": job["version"],
                          "slot": job["slot"], "retry": job["retry"] + 1})
    settle(backend, rid, fields, followups=followups)


def analyze_all(backend, parser, id=T):
    """The Trace Parser works through every queued ping."""
    for a in ids(backend, id, "analyze"):
        if backend.store.job(a)["status"] == "READY":
            parser.process(claim(backend, a))


def build(backend, version, sha, name="queue-drain-bug"):
    generate(backend, job_id(T, version, "create" if version == 1 else "revise"), sha, name)
    validate(backend, job_id(T, version, "validate", 0))


def score(backend, parser, version, pattern):
    for slot, mark in enumerate(pattern, start=1):
        run(backend, rollout_job_id(T, version, slot, 0), mark)
    analyze_all(backend, parser)


# --- submission --------------------------------------------------------------


def test_create_task_writes_the_task_and_its_create_job(backend):
    assert create_task(backend, task_id=T) == (T, True)
    t = task(backend)
    assert (t["status"], t["stage"], t["current_version"]) == ("CREATED", "QUEUED", 1)
    create = backend.store.job(job_id(T, 1, "create"))
    assert (create["type"], create["status"], create["input"]["origin"]) == ("create", "READY", "initial")
    assert create["input"]["spec"] == t["slot"] and t["slot"]["taxonomy"]["domain"]


def test_create_task_is_idempotent_and_conflicts_on_a_different_request(backend):
    create_task(backend, prompt="idea", task_id=T)
    assert create_task(backend, prompt="idea", task_id=T) == (T, False)
    assert len(ids(backend)) == 1
    with pytest.raises(TaskConflict):
        create_task(backend, prompt="another idea", task_id=T)
    assert len(backend.store.tasks()) == 1


def test_create_batch_writes_labelled_tasks_with_diverse_specs(backend):
    create_task(backend, task_id=T)
    batch_id, task_ids, created = create_batch(backend, 4, batch_id="b-x")
    assert (batch_id, created) == ("b-x", True) and task_ids == [f"b-x-t0{i}" for i in range(1, 5)]
    tasks = [task(backend, t) for t in [T, *task_ids]]
    assert [t["batch_id"] for t in tasks] == [None, "b-x", "b-x", "b-x", "b-x"]
    combos = {tuple(t["slot"]["taxonomy"][d] for d in UNIQUE_DIMENSIONS) for t in tasks}
    assert len(combos) == 5  # spread across the earlier task and each other
    for t in task_ids:
        assert backend.store.job(job_id(t, 1, "create"))["status"] == "READY"


def test_create_batch_is_idempotent_and_all_or_nothing(backend):
    first = create_batch(backend, 2, batch_id="b-x", prompt="idea")
    assert create_batch(backend, 2, batch_id="b-x", prompt="idea") == (*first[:2], False)
    with pytest.raises(BatchConflict):
        create_batch(backend, 3, batch_id="b-x")
    with pytest.raises(BatchConflict):
        create_batch(backend, 2, batch_id="b-x", prompt="another idea")
    create_task(backend, task_id="b-y-t02")
    with pytest.raises(BatchConflict):
        create_batch(backend, 2, batch_id="b-y")  # t01 must not be left behind
    with pytest.raises(ValueError):
        create_batch(backend, 51)
    assert sorted(t["id"] for t in backend.store.tasks()) == ["b-x-t01", "b-x-t02", "b-y-t02"]


def test_sampling_spreads_across_every_task_so_far():
    existing = []
    for _ in range(12):
        existing.append(sample_spec(existing)["taxonomy"])
    combos = {tuple(t[d] for d in UNIQUE_DIMENSIONS) for t in existing}
    assert len(combos) == 12
    assert len({t["domain"] for t in existing}) == len(TAXONOMY["domain"])  # every domain before any repeats


# --- the happy path: each service hands off to the next --------------------------


def test_accepts_in_band_and_publishes(backend, parser):
    create_task(backend, task_id=T)
    create = claim(backend, job_id(T, 1, "create"))
    handoffs.generation_started(backend)(create)
    assert (task(backend)["status"], task(backend)["stage"]) == ("CALIBRATING", "GENERATING")
    JobQueue(backend).release(create["id"], create["lease_token"])

    generate(backend, job_id(T, 1, "create"), "sha1")
    assert task(backend)["stage"] == "BUILDING" and task(backend)["name"] == "queue-drain-bug"
    val = backend.store.job(job_id(T, 1, "validate", 0))
    assert val["input"]["bundle_dir"].endswith("bundles/sha1") and val["input"]["round"] == 0

    validate(backend, val["id"])
    assert task(backend)["stage"] == "EVALUATING"
    rollouts = [backend.store.job(rollout_job_id(T, 1, s, 0)) for s in range(1, 6)]
    assert all(r and r["input"]["task_dir"].endswith("bundles/sha1") and r["config_hash"] for r in rollouts)

    score(backend, parser, 1, "01010")
    t = task(backend)
    assert (t["status"], t["stage"], t["accepted_version"]) == ("ACCEPTED", "BAND_OK", 1)
    published = parser.accepted_dir / "queue-drain-bug"
    assert (published / "instruction.md").read_text() == "fix it (sha1)\n"
    text = (published / "verdict.md").read_text()
    assert "ACCEPTED: v1 passed 2/5 valid rollouts (band 1–3)" in text
    assert "FAILED tests/test_outputs.py::test_crash_recovery - AssertionError: lost job 1" in text
    # Every run carries the Trace Parser's metrics; every failure its review.
    for slot, mark in enumerate("01010", start=1):
        r = backend.store.job(rollout_job_id(T, 1, slot, 0))
        assert r["trace_metrics"] is not None
        assert (r["failure"] is None) == (mark == "1")
        if mark == "0":
            assert r["failure"]["attribution"] == "model" and r["failure"]["cited_evidence"] == ["E1"]
    # One ping decided; the rest found it already decided (and were closed with the task).
    actions = sorted((backend.store.job(a)["result"] or {}).get("action", backend.store.job(a)["status"])
                     for a in ids(backend, type="analyze"))
    assert actions.count("accept") == 1 and set(actions) <= {"accept", "wait", "stale", "DEAD"}


def test_publish_is_retried_after_a_crash(backend, parser, monkeypatch):
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    real_publish = backend.artifacts.publish

    def crash(*args):
        assert task(backend)["status"] == "ACCEPTED"  # the decision committed first
        raise RuntimeError("killed mid-copy")

    monkeypatch.setattr(backend.artifacts, "publish", crash)
    score(backend, parser, 1, "10000")
    assert not (parser.accepted_dir / "queue-drain-bug").exists()

    monkeypatch.setattr(backend.artifacts, "publish", real_publish)
    assert parser.publish_pending() == 1
    assert "ACCEPTED: v1 passed 1/5" in (parser.accepted_dir / "queue-drain-bug" / "verdict.md").read_text()
    assert parser.publish_pending() == 0  # already published: nothing is copied again


def test_two_accepted_tasks_with_one_name_both_publish(backend, parser):
    for tid in (T, "t-2"):
        create_task(backend, task_id=tid)
        sha = f"sha-{tid}"
        generate(backend, job_id(tid, 1, "create"), sha, name="same-name")
        validate(backend, job_id(tid, 1, "validate", 0))
        for slot, mark in enumerate("10000", start=1):
            settle(backend, rollout_job_id(tid, 1, slot, 0),
                   {"outcome": "VALID_PASS" if mark == "1" else "VALID_FAIL"},
                   followups=[analyze_job(backend.store.job(rollout_job_id(tid, 1, slot, 0)))])
        analyze_all(backend, parser, tid)
    assert sorted(p.name for p in parser.accepted_dir.iterdir() if p.name != ".staging") == \
        ["same-name", "same-name-t-2"]


# --- repairs -------------------------------------------------------------------


def test_failed_gate_gets_repaired_then_rejected(backend):
    create_task(backend, task_id=T)
    generate(backend, job_id(T, 1, "create"), "sha1")
    for k in (0, 1, 2):
        validate(backend, job_id(T, 1, "validate", k), ok=False, feedback=f"oracle failed ({k})")
        if k < 2:
            repair = backend.store.job(job_id(T, 1, "repair", k + 1))
            assert repair["type"] == "revise"
            assert repair["input"] == {"origin": "repair", "bundle_sha256": f"sha{k + 1}",
                                       "feedback": f"oracle failed ({k})"}
            assert task(backend)["stage"] == "GENERATING"
            generate(backend, repair["id"], f"sha{k + 2}")
            assert backend.store.job(job_id(T, 1, "validate", k + 1))["bundle_sha256"] == f"sha{k + 2}"
    t = task(backend)
    assert (t["status"], t["reason"], t["stage"]) == ("REJECTED", "invalid", "INVALID")


def test_failed_generation_rejects_invalid(backend):
    create_task(backend, task_id=T)
    generate(backend, job_id(T, 1, "create"), None, ok=False)
    t = task(backend)
    assert (t["status"], t["reason"]) == ("REJECTED", "invalid")
    assert ids(backend) == [job_id(T, 1, "create")]


# --- calibration -----------------------------------------------------------------


def test_too_easy_is_revised_until_exhausted(backend, parser):
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    score(backend, parser, 1, "11111")
    t = task(backend)
    assert (t["status"], t["current_version"], t["stage"]) == ("CALIBRATING", 2, "QUEUED")
    rev = backend.store.job(job_id(T, 2, "revise"))
    assert rev["type"] == "revise" and rev["input"]["origin"] == "revision"
    assert rev["input"]["bundle_sha256"] == "sha1"
    assert rev["input"]["operator"] == "increase_diagnosis_depth"
    assert rev["input"]["result"] == "5 of 5 runs passed."

    build(backend, 2, "sha2")
    score(backend, parser, 2, "11110")
    assert backend.store.job(job_id(T, 3, "revise"))["input"]["operator"] == "increase_constraint_interaction"

    # Every later version is revised too, until the last one allowed.
    for v in range(3, MAX_VERSIONS):
        build(backend, v, f"sha{v}")
        score(backend, parser, v, "11111")
        assert task(backend)["current_version"] == v + 1
    build(backend, MAX_VERSIONS, "sha-last")
    score(backend, parser, MAX_VERSIONS, "11111")
    t = task(backend)
    assert (t["status"], t["reason"], t["current_version"]) == ("REJECTED", "exhausted", MAX_VERSIONS)
    assert not backend.store.job(job_id(T, MAX_VERSIONS + 1, "revise"))  # no version past the cap
    assert not parser.accepted_dir.exists()


def test_too_easy_revision_carries_why_it_was_easy(backend, tmp_path):
    """5/5: the passing traces are reviewed and the revision is told why it was easy."""
    calls = []

    def complete(messages, response_format, llm_dir=None, cache_key=""):
        calls.append((response_format["json_schema"]["name"], cache_key))
        reply = {"why_easy": "Every run read the instruction's hint and patched drain() [E1].",
                 "simple_strategy": "patch drain() locally", "cues": [{"quote": "drain", "where": "instruction"}],
                 "fix_applied": "reordered the ack", "cited_evidence": ["E1"],
                 "suggestions": [{"technique": "preserve_guarantees", "change": "make the stats module rely on "
                                  "the old ordering", "defeats": "the local reorder breaks the stats"}]}
        return SimpleNamespace(content=json.dumps(reply), cost_usd=0.02, cached=False)

    parser = TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=complete)
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    score(backend, parser, 1, "11111")
    rev = backend.store.job(job_id(T, 2, "revise"))
    assert rev["input"]["direction"] == "harder"
    analysis = rev["input"]["analysis"]
    assert analysis["trusted"] and analysis["simple_strategy"] == "patch drain() locally"
    assert analysis["suggestions"][0]["technique"] == "preserve_guarantees"
    assert calls == [("pass_review", f"pass-review:{T}-v1")]  # one review per version, not per run
    decision = next(j for j in backend.store.task_jobs(T) if j["type"] == "analyze" and
                    (j["result"] or {}).get("action") == "revise")
    assert decision["result"]["easy_analysis"]["why_easy"].startswith("Every run")
    assert decision["cost_usd"] >= 0.02


def test_too_easy_revisions_mutate_the_world_and_keep_its_history(backend, tmp_path):
    """Each too-easy version picks mutations from the evidence; the history accumulates and
    successive versions try operators not applied before."""
    def complete(messages, response_format, llm_dir=None, cache_key=""):
        reply = {"why_easy": "Every run kept a seen-set [E1].", "simple_strategy": "dedupe in memory",
                 "cues": [{"quote": "twice", "where": "instruction"}],
                 "fix_applied": "a process-local set of processed job ids", "cited_evidence": ["E1"],
                 "suggestions": []}
        return SimpleNamespace(content=json.dumps(reply), cost_usd=0.02, cached=False)

    parser = TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=complete)
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    score(backend, parser, 1, "11111")
    v2 = backend.store.job(job_id(T, 2, "revise"))["input"]
    # Cues mean the instruction leaks: remove them first, then defeat the in-memory guard.
    assert v2["mutation_operators"] == ["remove_cues", "add_persistent_state"]
    assert v2["target_fix"] == "a process-local set of processed job ids"
    assert [m["version"] for m in v2["mutations"]] == [2]
    assert [r["rule"] for r in v2["mutations"][0]["rules"]] == ["cues_found", "process_local_guard"]
    assert v2["mutations"][0]["rules"][1]["evidence"] == 'pass review said "process-local"'

    build(backend, 2, "sha2")
    score(backend, parser, 2, "11110")
    v3 = backend.store.job(job_id(T, 3, "revise"))["input"]
    assert [m["version"] for m in v3["mutations"]] == [2, 3]
    # Still leaking: remove_cues again; the other operator is one not tried yet.
    assert v3["mutation_operators"][0] == "remove_cues"
    assert v3["mutation_operators"][1] not in v2["mutation_operators"]
    decision = next(j for j in backend.store.task_jobs(T) if j["type"] == "analyze" and j["version"] == 2
                    and (j["result"] or {}).get("action") == "revise")
    assert decision["result"]["mutation"]["operators"] == v3["mutation_operators"]


def test_too_hard_and_repair_revisions_mutate_nothing(backend, parser):
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    score(backend, parser, 1, "00000")
    rev = backend.store.job(job_id(T, 2, "revise"))["input"]
    assert rev["direction"] == "easier" and rev["mutation_operators"] == []
    assert rev["mutations"][0]["direction"] == "easier"


def _next(direction, world_sha="w1", operators=("add_persistent_state",)):
    from evaluation.trace_parser_service import next_job

    mutation = {"version": 2, "from_version": 1, "pass_count": 5, "direction": direction,
                "operators": list(operators), "why": ["w"] * len(operators), "target_fix": "a seen-set"}
    return next_job({"id": T, "slot": {"family": "database_consistency"}}, 1, "sha1", world_sha,
                    direction=direction, operator="increase_diagnosis_depth", mutation=mutation,
                    history=[{"version": 1, "operators": []}], analysis={"why_easy": "x"},
                    result="5 of 5 runs passed.", evidence="- run 1: passed")


def test_agent_mode_too_easy_mutates_the_world(monkeypatch):
    """Plan §3.6: too easy → a `world` mutation job from the parent world."""
    monkeypatch.setenv("SDE_AUTHOR_MODE", "agent")
    job = _next("harder")
    assert job["type"] == "world" and job["id"] == job_id(T, 2, "world") == f"{T}-v2-world"
    inp = job["input"]
    assert inp["origin"] == "mutation" and inp["parent_world_sha256"] == "w1"
    assert inp["mutation"] == {"operators": ["add_persistent_state"], "why": ["w"]}
    assert [m["version"] for m in inp["mutations"]] == [1, 2] and inp["target_fix"] == "a seen-set"
    # The hand-off copies shape and dial into create, so they are always present.
    assert inp["spec"] == {"family": "database_consistency", "shape": "fix", "dial": 2}
    assert inp["analysis"] == {"why_easy": "x"}


def test_agent_mode_too_hard_and_blamed_revise_the_task_on_the_same_world(monkeypatch):
    monkeypatch.setenv("SDE_AUTHOR_MODE", "agent")
    easier, blamed = _next("easier", operators=()), _next("repair", operators=())
    assert easier["type"] == "revise" and easier["input"]["origin"] == "easier"
    assert easier["input"]["world_sha256"] == "w1" and easier["input"]["bundle_sha256"] == "sha1"
    assert blamed["input"]["origin"] == "repair_ambiguity"


def test_one_shot_mode_or_a_task_without_a_world_revises_one_shot(monkeypatch):
    monkeypatch.setenv("SDE_AUTHOR_MODE", "agent")
    job = _next("harder", world_sha=None)  # written one-shot before agent mode was turned on
    assert job["type"] == "revise" and job["input"]["origin"] == "revision"
    assert job["input"]["mutation_operators"] == ["add_persistent_state"]
    monkeypatch.setenv("SDE_AUTHOR_MODE", "oneshot")
    assert _next("easier")["input"]["origin"] == "revision"


def test_verdict_shows_mutations_and_model_judged_gates():
    from evaluation import verdict

    hist = [{"version": 2, "from_version": 1, "pass_count": 5, "direction": "harder",
             "operators": ["add_persistent_state"], "why": ["a restart must defeat it"], "target_fix": "a seen-set"}]
    job = {"cost_usd": None, "outcome": "OK", "bundle_sha256": "abc", "slot": None, "retry": 0}
    jobs = [{**job, "id": "v2", "type": "revise", "version": 2,
             "input": {"origin": "revision", "mutation_operators": ["add_persistent_state"], "mutations": hist}},
            {**job, "id": "v2-validate", "type": "validate", "version": 2,
             "result": {"gates": {"wrong_fix": {"passed": True}, "leak": {"passed": False}}}}]
    text = verdict.render({"id": T, "current_version": 2, "slot": {}}, jobs, "ACCEPTED", 5)
    assert "| 2 | revision | - | add_persistent_state | - | `abc` | OK (wrong_fix ✓, leak ✗) |" in text
    jobs += [{**job, "id": "v2-world", "type": "world", "version": 2, "result": {"world_sha256": "f" * 64}},
             {**job, "id": "v2-wc", "type": "world_check", "version": 2, "status": "DONE"}]
    text = verdict.render({"id": T, "current_version": 2, "slot": {}}, jobs, "ACCEPTED", 5)
    assert f"| `{'f' * 12}` (OK) |" in text
    assert "- v1 → v2 (5/5 passed, harder): add_persistent_state" in text and "aimed at: a seen-set" in text


def test_choose_mutations_reads_the_passing_strategy():
    from evaluation.calibration import MUTATION_OPERATORS, choose_mutations

    def run(slot, files=None, steps=None, cmds=()):
        return {"slot": slot, "outcome": "VALID_PASS",
                "trace_metrics": {"files_modified": files, "llm_calls": steps,
                                  "tool_calls": [{"input": c} for c in cmds]}}

    greedy = choose_mutations(5, [run(1, ["a.py", "b.py"], 30, ["sed -i 's/x/y/' a.py"])],
                              {"trusted": True, "simple_strategy": "a greedy first-fit assignment"}, [])
    assert greedy["operators"][0] == "generate_counterexample"
    # Untrusted analysis is ignored; one-file, few-step passes still say what to change.
    local = choose_mutations(4, [run(i, ["a.py"], 5) for i in range(1, 5)] + [{"slot": 5, "outcome": "VALID_FAIL"}],
                             {"trusted": False, "fix_applied": "a lock"}, [])
    assert local["operators"] == ["extend_cross_component_dependency", "add_realistic_distractor"]
    assert local["rules"][0] == {"operator": "extend_cross_component_dependency", "rule": "one_file_patch",
                                 "evidence": "median files edited 1"}
    assert local["target_fix"] is None
    # Commands match only code patterns: the word "lock" in a grep doesn't fire, a Lock() does.
    plain = choose_mutations(5, [run(1, ["a.py", "b.py"], 30, ["grep -n lock app.py"])], None, [])
    assert "add_concurrent_execution" not in plain["operators"]
    coded = choose_mutations(5, [run(1, ["a.py", "b.py"], 30, ["cat >> app.py <<EOF\nl = threading.Lock()\nEOF"])],
                             None, [])
    assert coded["operators"][0] == "add_concurrent_execution"
    assert coded["rules"][0]["evidence"] == "command contained `threading.Lock(`"
    # Nothing specific: global invariants and counterexamples, not size; suggestions break ties.
    assert plain["operators"] == ["introduce_global_invariant", "generate_counterexample"]
    tip = choose_mutations(5, [run(1, ["a.py", "b.py"], 30)],
                           {"trusted": True, "suggestions": [{"technique": "genuine_tradeoff", "change": "c"}]}, [])
    assert tip["operators"][0] == "tighten_resource_bound" and tip["rules"][0]["rule"] == "pass_review_suggestion"
    # Agents that fuzzed their own fix: a requirement their self-check can't see.
    fuzz = choose_mutations(5, [run(1, ["a.py", "b.py"], 30)],
                            {"trusted": True, "simple_strategy": "wrote a differential fuzzer and iterated"}, [])
    assert fuzz["operators"] == ["introduce_global_invariant", "generate_counterexample"]
    assert fuzz["rules"][0]["rule"] == "self_checking_agent"
    # aws-agent-b1-t02: every pass fixed stale cache entries in one file. A cache-invalidation
    # fix is a one-file patch, not a process-local guard (no add_persistent_state).
    stale = choose_mutations(5, [run(i, ["engine.py"], 13) for i in range(1, 6)],
                             {"trusted": True, "cues": [{"quote": "schedules containing redefinitions"}],
                              "fix_applied": "invalidate the engine's cached results when a name is redefined"}, [])
    assert stale["operators"] == ["remove_cues", "extend_cross_component_dependency"]
    assert stale["rules"][1]["rule"] == "one_file_patch"
    # aws-agent-b1-t08: the greedy heuristic was replaced by a search over the set of jobs;
    # "set of" names data, not a guard.
    greedy_set = choose_mutations(5, [run(i, ["planner.c", "solver.c", "report.c"], 20) for i in range(1, 6)],
                                  {"trusted": True, "cues": [{"quote": "a smaller feasible collection"}],
                                   "fix_applied": "replaced the greedy value/duration heuristic with a search "
                                                  "over every subset of the set of jobs"}, [])
    assert greedy_set["operators"] == ["remove_cues", "generate_counterexample"]
    assert greedy_set["rules"][1]["rule"] == "locally_plausible_strategy"
    # ...and its commands wrote C that mentions "the global optimum": not a Python `global`.
    c_text = ["cat > planner.c <<EOF\n/* the global optimum over all subsets */\nEOF"]
    greedy_c = choose_mutations(5, [run(i, ["planner.c", "solver.c"], 20, c_text) for i in range(1, 6)],
                                {"trusted": True, "fix_applied": "replaced the greedy heuristic with an exact search"}, [])
    assert "add_persistent_state" not in greedy_c["operators"]
    py_global = choose_mutations(5, [run(1, ["a.py", "b.py"], 30, ["cat >> w.py <<EOF\ndef f():\n    global SEEN\nEOF"])],
                                 None, [])
    assert py_global["operators"][0] == "add_persistent_state"
    # Already applied operators go last; everything chosen is from the contract's list.
    again = choose_mutations(4, [run(i, ["a.py"], 5) for i in range(1, 5)], None,
                             [{"operators": ["extend_cross_component_dependency"]}])
    assert "extend_cross_component_dependency" not in again["operators"]
    assert set(greedy["operators"] + local["operators"] + again["operators"]) <= set(MUTATION_OPERATORS)


def test_in_band_and_too_hard_versions_get_no_pass_review(backend, tmp_path):
    names = []

    def complete(messages, response_format, llm_dir=None, cache_key=""):
        names.append(response_format["json_schema"]["name"])
        return review()(messages, response_format, llm_dir, cache_key)

    parser = TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=complete)
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    score(backend, parser, 1, "00000")
    assert "pass_review" not in names
    assert backend.store.job(job_id(T, 2, "revise"))["input"]["direction"] == "easier"


def test_too_hard_is_made_easier_then_accepted_at_v2(backend, parser):
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    score(backend, parser, 1, "00000")
    rev = backend.store.job(job_id(T, 2, "revise"))
    assert rev["input"]["operator"] == "localize_diagnosis"
    # The revision prompt gets the review of every run, quoting the verifier.
    assert "- run 3: failed: FAILED tests/test_outputs.py::test_crash_recovery - AssertionError: lost job 3 [E1]" \
        in rev["input"]["evidence"]
    build(backend, 2, "sha2")
    score(backend, parser, 2, "00100")
    t = task(backend)
    assert (t["status"], t["accepted_version"]) == ("ACCEPTED", 2)
    assert "ACCEPTED: v2 passed 1/5" in (parser.accepted_dir / "queue-drain-bug" / "verdict.md").read_text()


def test_a_failure_blamed_on_the_task_is_repaired_not_measured(backend, tmp_path):
    parser = TraceParserService(backend, accepted_dir=tmp_path / "accepted", complete=review("task"))
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    score(backend, parser, 1, "01010")  # in band, but a failure is the task's fault
    t = task(backend)
    assert (t["status"], t["current_version"]) == ("CALIBRATING", 2)
    rev = backend.store.job(job_id(T, 2, "revise"))
    assert rev["input"]["operator"] == "repair_ambiguity"


# --- invalid and dead runs ----------------------------------------------------------


def test_invalid_runs_are_retried_then_unresolved(backend, parser):
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    for retry in range(4):
        run(backend, rollout_job_id(T, 1, 1, retry), "i")
        analyze_all(backend, parser)
        if retry < 3:
            assert backend.store.job(rollout_job_id(T, 1, 1, retry + 1))["status"] == "READY"
            assert task(backend)["stage"] == "EVALUATING"
    t = task(backend)
    assert (t["status"], t["reason"], t["stage"]) == ("REJECTED", "infra_unresolved", "UNRESOLVED")


def test_rejecting_a_task_cancels_its_open_jobs(backend, parser):
    """Slot 1 is out of retries while slots 2–5 are still queued or running:
    the task is rejected and the rest of its work is cancelled, not left orphaned."""
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    claim(backend, rollout_job_id(T, 1, 2, 0))  # a worker is mid-trial
    for retry in range(4):
        run(backend, rollout_job_id(T, 1, 1, retry), "i")
    analyze_all(backend, parser)
    assert (task(backend)["status"], task(backend)["reason"]) == ("REJECTED", "infra_unresolved")
    for slot in (2, 3, 4, 5):
        j = backend.store.job(rollout_job_id(T, 1, slot, 0))
        assert (j["status"], j["error"]) == ("DEAD", "cancelled: task REJECTED (infra_unresolved)")
    assert backend.store.open_job_counts() == {"ready": 0, "leased": 0, "expired": 0}


def test_a_dead_run_is_retried_by_the_trace_parser_once(backend, parser):
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    rid = rollout_job_id(T, 1, 2, 0)
    settle(backend, rid, {"status": "DEAD", "error": "dead-lettered"})
    with backend.store.tx() as c:  # what the reconciler adds for a dead-lettered run
        insert_job(c, id=f"{rid}-analyze", task_id=T, version=1, type=JobType.ANALYZE, input={"trigger": rid})
    analyze_all(backend, parser)
    analyze_all(backend, parser)
    assert ids(backend).count(rollout_job_id(T, 1, 2, 1)) == 1
    # A scored retry counts for its slot; the dead attempt never does.
    run(backend, rollout_job_id(T, 1, 2, 1), "1")
    for slot in (1, 3, 4, 5):
        run(backend, rollout_job_id(T, 1, slot, 0), "0")
    analyze_all(backend, parser)
    assert (task(backend)["status"], task(backend)["accepted_version"]) == ("ACCEPTED", 1)


# --- concurrency -------------------------------------------------------------------


def test_many_pings_and_two_parsers_decide_a_version_once(backend, tmp_path):
    create_task(backend, task_id=T)
    build(backend, 1, "sha1")
    for slot in range(1, 6):
        run(backend, rollout_job_id(T, 1, slot, 0), "1")  # 5/5: too easy → one revision
    parsers = [TraceParserService(from_env({"SDE_WORK_DIR": str(tmp_path / "work")}),
                                  accepted_dir=tmp_path / "accepted", complete=review()) for _ in range(2)]

    def drain(p):
        jq = JobQueue(p.backend)
        while (job := jq.claim(JobType.ANALYZE, "host:1:a")) is not None:
            p.process(job)

    threads = [threading.Thread(target=drain, args=(p,)) for p in parsers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert ids(backend, type="revise") == [job_id(T, 2, "revise")]
    t = task(backend)
    assert (t["current_version"], t["stage"]) == (2, "QUEUED")
    actions = [backend.store.job(a)["result"]["action"] for a in ids(backend, type="analyze")]
    assert actions.count("revise") == 1 and set(actions) <= {"revise", "stale"}


def test_generate_rows_from_before_the_split_are_migrated(backend, tmp_path):
    create_task(backend, task_id=T)
    with backend.store.tx() as c:
        for jid, origin in ((f"{T}-v1-generate", "initial"), (f"{T}-v1-repair1", "repair"),
                            (f"{T}-v2-generate", "revision")):
            c.execute("""INSERT INTO jobs (id, task_id, version, type, input, status, due_at, priority, created_at)
                         VALUES (?, ?, 1, 'generate', ?, 'READY', 0, 10, '')""",
                      (jid, T, json.dumps({"origin": origin})))
    migrated = from_env({"SDE_WORK_DIR": str(tmp_path / "work")})  # schema init runs the migration
    types = {j["id"]: j["type"] for j in migrated.store.task_jobs(T)}
    assert types[f"{T}-v1-generate"] == "create"
    assert types[f"{T}-v1-repair1"] == types[f"{T}-v2-generate"] == "revise"
    assert reconcile(migrated)["dead_lettered"] == 0
