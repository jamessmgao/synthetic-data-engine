"""
The whole pipeline, end to end, as separate processes on a real backend:

    operator ─HTTP─► API ─► store / queue / artifacts ◄─► Generation Service (create + revise)
                                                          Build + Validate
                                                          Executor
                                                          Trace Parser
                                                          reconciler

Each service hands off to the next stage in its own settle transaction; the
Trace Parser accepts, revises or rejects. Only GLM-5.1 (drafting and the failure
review) and Harbor are replaced (tests/e2e/fakes.py). The batch below hits
every path a task can take; each test checks the exact set of jobs the
pipeline created, every task's verdict, what was published, the API's report,
and that no paid unit of work (model call or trial) ever ran to completion twice.
"""

import pytest

from shared.storage.sql import job_id, rollout_job_id

# What each task's fakes do. Marks: 1 pass, 0 fail, i invalid once then pass, I always invalid.
SCENARIO = {
    "t01": {"versions": {"1": "01010"}},                                # accepted at v1, 2/5
    "t02": {"versions": {"1": "11111", "2": "01100"}},                  # too easy → revised → accepted at v2
    "t03": {"gate_failures": 3},                                        # gates fail, 2 repairs, rejected invalid
    "t04": {"versions": {"1": "00000", "2": "00000", "3": "00000"}},    # too hard 3 times → exhausted
    "t05": {"versions": {"1": "1i000"}},                                # a timeout retried, never scored
    "t06": {"versions": {"1": "10I00"}},                                # slot 3 never valid → infra_unresolved
    "t07": {"generate": "fail"},                                        # no acceptable draft → invalid
    "t08": {"gate_failures": 1, "versions": {"1": "00100"}},            # repaired once, then accepted at 1/5
}

EXPECTED = {  # task -> (status, reason, accepted_version, current_version)
    "t01": ("ACCEPTED", None, 1, 1),
    "t02": ("ACCEPTED", None, 2, 2),
    "t03": ("REJECTED", "invalid", None, 1),
    "t04": ("REJECTED", "exhausted", None, 3),
    "t05": ("ACCEPTED", None, 1, 1),
    "t06": ("REJECTED", "infra_unresolved", None, 1),
    "t07": ("REJECTED", "invalid", None, 1),
    "t08": ("ACCEPTED", None, 1, 1),
}

EXPECTED_REPORT = {
    "candidates": 8,
    "structural_pass": 7,      # every task that reached validation (all but t07)
    "functional_pass": 6,      # every task with a passing validate (all but t03, t07)
    "accepted_v1": 3,
    "accepted_total": 4,
    "rejected": {"invalid": 2, "exhausted": 1, "infra_unresolved": 1},
    "in_progress": 0,
    # versions with 5 scored runs: t01 2, t02 5+2, t04 0+0+0, t05 2, t08 1 (t06 never got 5)
    "pass_count_histogram": {"0": 3, "1": 1, "2": 3, "3": 0, "4": 0, "5": 1},
    "valid_rollouts": 44,
    "invalid_rollouts": 5,     # t05 slot 2 once, t06 slot 3 four times
}


def expected_job_ids(scenario: dict) -> set[str]:
    """Every job the pipeline must create for this scenario, and no others,
    apart from the analyze pings (one per settled run; see check_outcome)."""
    ids = set()
    for suffix, sc in scenario.items():
        t = f"b-e2e-{suffix}"
        for v in (1, 2, 3):
            ids.add(job_id(t, v, "create" if v == 1 else "revise"))
            if sc.get("generate") == "fail":
                break
            for k in range(3):
                ids.add(job_id(t, v, "validate", k))
                if k >= sc.get("gate_failures", 0):
                    break
                if k < 2:
                    ids.add(job_id(t, v, "repair", k + 1))
            else:
                break  # three failed gates: rejected invalid
            if sc.get("gate_failures", 0) >= 3:
                break
            pattern = sc.get("versions", {}).get(str(v), "01000")
            for slot, mark in enumerate(pattern, start=1):
                tries = {"i": 2, "I": 4}.get(mark, 1)
                ids |= {rollout_job_id(t, v, slot, r) for r in range(tries)}
            if "I" in pattern:
                break
            if 1 <= sum(m in "1i" for m in pattern) <= 3:
                break
    return ids


def check_outcome(p, scenario=SCENARIO, expected=EXPECTED):
    """The batch finished exactly as scripted, with no work lost or duplicated."""
    batch = p.api("GET", "/v1/batches/b-e2e")
    got = {t["id"].rsplit("-", 1)[1]: t for t in batch["tasks"]}
    for suffix, (status, reason, accepted_version, version) in expected.items():
        t = got[suffix]
        assert (t["status"], t["reason"], t["current_version"]) == (status, reason, version), suffix
        verdict = p.api("GET", f"/v1/tasks/b-e2e-{suffix}")
        assert verdict["task"]["accepted_version"] == accepted_version, suffix

    # Exactly the scripted jobs: nothing missing, nothing duplicated, all settled.
    # Every run that settled pinged the Trace Parser exactly once. A decided
    # task's leftover work (slots still queued or running, pings not yet
    # analyzed) is cancelled when it is decided, never left open.
    jobs = p.jobs()
    work = [j for j in jobs if j["type"] != "analyze"]
    assert sorted(j["id"] for j in work) == sorted(expected_job_ids(scenario))
    settled_runs = {j["id"] for j in work if j["type"] == "rollout" and j["status"] == "DONE"}
    assert sorted(j["id"] for j in jobs if j["type"] == "analyze") == sorted(f"{r}-analyze" for r in settled_runs)
    for j in jobs:
        cancelled = j["status"] == "DEAD" and (j["error"] or "").startswith(
            f"cancelled: task {expected[j['task_id'].rsplit('-', 1)[1]][0]}")
        assert j["status"] == "DONE" or cancelled, (j["id"], j["status"], j["error"])
    jobs = [j for j in work if j["status"] == "DONE"]

    # Paid work (model calls, trials) is never repeated, with one exception the
    # PRD allows (N2): work a killed process had finished but not yet settled,
    # reclaimed on a *different machine* that can't see the killed one's disk.
    # On one machine the restart adopts it, so there it must be zero.
    repeats = 0
    for kind, prefix in (("generate", ("-create", "-revise", "-repair")), ("trial", ("-rollout",))):
        log = p.event_log(kind)
        for j in jobs:
            if not any(s in j["id"] for s in prefix):
                continue
            events = log.get(j["id"], [])
            finishes = [pid for e, pid in events if e == "finish"]
            assert any(e in ("finish", "adopt") for e, _ in events), f"{j['id']} settled without running"
            if len(finishes) > 1:
                assert len([f for f in finishes if f not in p.killed]) <= 1, f"{j['id']} repeated: {events}"
                assert j["attempts"] >= 2, f"{j['id']} repeated without being reclaimed: {events}"
                repeats += len(finishes) - 1
    if len(p.work_dirs) == 1:
        assert repeats == 0, "a restart on the same machine must adopt finished work"
    # At most what the killed processes had in flight (8 workers each).
    assert repeats <= 8 * len(p.killed)
    p.repeats = repeats

    # Only accepted tasks are published, once each, with their verdict.
    accepted = sorted(f"fake-{s}" for s, e in expected.items() if e[0] == "ACCEPTED")
    entries = sorted(d.name for d in p.accepted.iterdir()) if p.accepted.exists() else []
    # Publishing stages under .staging/ and renames from there, so a kill mid-copy
    # leaves nothing else behind.
    assert [e for e in entries if e != ".staging"] == accepted
    for name in accepted:
        suffix = name.removeprefix("fake-")
        text = (p.accepted / name / "verdict.md").read_text()
        assert text.startswith(f"# Verdict: b-e2e-{suffix}\n")
        assert f"ACCEPTED: v{expected[suffix][2]} passed" in text
        assert (p.accepted / name / "scenario.json").exists()  # the bundle itself


# --- a calm run -------------------------------------------------------------------


def test_batch_runs_every_path_to_the_scripted_verdict(make_pipeline):
    p = make_pipeline(SCENARIO)
    submitted = p.submit()
    assert submitted["task_ids"] == [f"b-e2e-{s}" for s in SCENARIO] and submitted["created"]
    p.wait(p.start())
    check_outcome(p)

    # Nothing was interrupted, so nothing was adopted or re-run.
    for kind in ("generate", "trial"):
        assert all(e == {"start": 1, "finish": 1} for e in p.events(kind).values())

    report = p.api("GET", "/v1/batches/b-e2e/report")
    assert {k: report[k] for k in EXPECTED_REPORT} == EXPECTED_REPORT
    n_generate = sum(1 for j in p.jobs() if j["type"] in ("create", "revise"))
    n_trials = sum(1 for j in p.jobs() if j["type"] == "rollout")
    assert report["cost_usd"]["authoring"] == pytest.approx(0.25 * n_generate)
    assert report["cost_usd"]["evaluation"] == pytest.approx(0.012 * n_trials)
    assert report["cost_usd"]["per_accepted"] == pytest.approx(report["cost_usd"]["total"] / 4)
    assert isinstance(report["config_hash"], str)  # every rollout measured under one config
    assert report["wall_clock_s"] is not None


def test_verdicts_explain_every_step(make_pipeline):
    p = make_pipeline({"t01": SCENARIO["t02"], "t02": SCENARIO["t04"], "t03": SCENARIO["t05"]})
    p.submit()
    p.wait(p.start())

    revised = p.api("GET", "/v1/tasks/b-e2e-t01")
    assert revised["decision"] == "ACCEPTED: v2 passed 2/5 valid rollouts (band 1–3)"
    v1, v2 = revised["versions"]
    assert (v1["origin"], v1["state"], v1["pass_count"]) == ("initial", "OUT_OF_BAND", 5)
    assert (v2["origin"], v2["operator"], v2["state"]) == ("revision", "increase_diagnosis_depth", "BAND_OK")
    assert v1["bundle_sha256"] != v2["bundle_sha256"]
    assert v2["gates"]["oracle1"]["passed"] is True

    # The too-hard task got easier operators, each tried once, and the evidence
    # handed to the model is the Trace Parser's review of each failed run, which
    # quotes the failing test from the verifier output (fetched from S3 on the cluster).
    exhausted = p.backend.store.task_jobs("b-e2e-t02")
    revisions = [j["input"] for j in exhausted if j["type"] == "revise" and j["input"]["origin"] == "revision"]
    assert [r["operator"] for r in revisions] == ["localize_diagnosis", "reduce_unrelated_constraints"]
    assert revisions[0]["result"] == "0 of 5 runs passed."
    assert "test_crash_recovery - AssertionError: slot 1 lost a job" in revisions[0]["evidence"]
    failure = next(r for r in p.api("GET", "/v1/tasks/b-e2e-t02")["rollouts"] if r["version"] == 1)["failure"]
    assert failure["attribution"] == "model" and failure["cited_evidence"] == ["E1"]

    # The retried timeout is on the record, and not counted.
    retried = p.api("GET", "/v1/tasks/b-e2e-t03")
    slot2 = [(r["retry"], r["outcome"], r["error"]) for r in retried["rollouts"] if r["slot"] == 2]
    assert slot2 == [(0, "INVALID", "AgentTimeoutError: fake timeout"), (1, "VALID_PASS", None)]
    assert retried["versions"][0]["pass_count"] == 2


def test_resubmitting_a_finished_batch_changes_nothing(make_pipeline):
    p = make_pipeline({"t01": SCENARIO["t01"]})
    p.submit()
    p.wait(p.start())
    before = sorted((j["id"], j["status"]) for j in p.jobs())
    assert p.submit()["created"] is False
    r = p.client.post("/v1/batches", json={"count": 2, "batch_id": "b-e2e"}, headers={"X-Api-Key": "e2e-key"})
    assert r.status_code == 409
    p.wait(p.start())  # a second run finds nothing to do and exits
    assert sorted((j["id"], j["status"]) for j in p.jobs()) == before


# --- kill, restart, two copies --------------------------------------------------------


def test_kill_dash_nine_mid_batch_then_restart(make_pipeline):
    p = make_pipeline(SCENARIO)
    p.submit()
    total = len(expected_job_ids(SCENARIO))
    first = p.start(step_s=0.15)
    p.wait_until(lambda: p.settled_count() >= total // 4)
    p.kill(first)                        # mid-batch: leases held, runs half done
    second = p.start(step_s=0.15)        # restart on the same machine
    p.wait_until(lambda: p.settled_count() >= total * 2 // 3)
    p.kill(second)                       # and again
    p.wait(p.start(step_s=0.15))
    check_outcome(p)


def test_two_copies_at_once(make_pipeline):
    p = make_pipeline(SCENARIO)
    p.submit()
    # On the cluster the copies get separate work dirs: two machines sharing
    # Postgres, Redis and S3. On SQLite they share one.
    a = p.start("m1")
    b = p.start("m2" if p.kind == "cluster" else "m1")
    p.wait(a, b)
    check_outcome(p)
    assert len(p.pids("trial")) == 2, "both copies should have run trials"


def test_two_copies_one_killed_and_restarted(make_pipeline):
    p = make_pipeline(SCENARIO)
    p.submit()
    other = "m2" if p.kind == "cluster" else "m1"
    a, b = p.start("m1", step_s=0.15), p.start(other, step_s=0.15)
    p.wait_until(lambda: p.settled_count() >= len(expected_job_ids(SCENARIO)) // 3)
    p.kill(a)
    a2 = p.start("m1", step_s=0.15)
    p.wait(a2, b)
    check_outcome(p)


# --- the API as a server, poison jobs, and the cluster's own failure modes ------------


def test_no_model_endpoint_is_reachable_from_the_tests():
    """netguard refuses anything off this machine, including an OpenAI-compatible
    client pointed at a real host. (TEST-NET addresses: nothing is ever sent.)"""
    import socket

    import netguard
    from openai import APIConnectionError, OpenAI

    with pytest.raises(netguard.ExternalNetworkBlocked):
        socket.create_connection(("203.0.113.10", 443), timeout=1)
    client = OpenAI(api_key="fake", base_url="http://203.0.113.10/v1", max_retries=0, timeout=2)
    with pytest.raises(APIConnectionError):
        client.chat.completions.create(model="z-ai/glm-5.1", messages=[{"role": "user", "content": "hi"}])
    assert netguard._local(("127.0.0.1", 5432)) and netguard._local(("::1", 6379))  # the cluster stays reachable


def _healthy(http) -> bool:
    import httpx

    try:
        return http.get("/healthz").json()["status"] == "ok"
    except httpx.HTTPError:
        return False


def test_operator_drives_a_batch_over_http(make_pipeline):
    """`python -m api` as its own process: submit, watch and report over HTTP."""
    import httpx

    scenario = {"t01": SCENARIO["t01"], "t02": SCENARIO["t02"], "t03": SCENARIO["t03"]}
    p = make_pipeline(scenario)
    server, url = p.start_api_server()
    auth = {"X-Api-Key": "e2e-key"}
    with httpx.Client(base_url=url, timeout=5) as http:
        p.wait_until(lambda: _healthy(http), timeout=30)
        assert http.get("/v1/batches").status_code == 401
        r = http.post("/v1/batches", json={"count": 3, "batch_id": "b-e2e"}, headers=auth)
        assert r.status_code == 202, r.text

        stack = p.start()
        p.wait_until(lambda: all(t["status"] in ("ACCEPTED", "REJECTED")
                                 for t in http.get("/v1/batches/b-e2e", headers=auth).json()["tasks"]))
        p.wait(stack)
        report = http.get("/v1/batches/b-e2e/report", headers=auth).json()
        assert (report["accepted_total"], report["rejected"]) == (2, {"invalid": 1})
        assert http.get("/v1/batches", headers=auth).json()[0]["accepted"] == 2
        verdict = http.get("/v1/tasks/b-e2e-t02", headers=auth).json()
        assert verdict["decision"].startswith("ACCEPTED: v2 passed 2/5")
    server.terminate()
    server.wait()


def test_poison_job_is_dead_lettered_not_retried_forever(make_pipeline):
    """A job that kills its worker every time it runs: the reconciler dead-letters it
    after MAX_JOB_ATTEMPTS claims and rejects its task in the same transaction (no
    service would ever hand that version on), the rest finish.
    Jobs that shared the process with it are charged for one crash at most: after
    that the suspect runs isolated (shared.storage.sql.claim)."""
    p = make_pipeline({"t01": {"generate": "crash"}, "t02": SCENARIO["t01"]})
    p.submit()
    crashes = p.run_until_final()
    assert crashes == 3  # shared.contracts.models.MAX_JOB_ATTEMPTS

    poison = p.backend.store.job(job_id("b-e2e-t01", 1, "create"))
    assert (poison["status"], poison["attempts"]) == ("DEAD", 3)
    assert poison["error"].startswith("dead-lettered")
    t1, t2 = (p.backend.store.task(f"b-e2e-{s}") for s in ("t01", "t02"))
    assert (t1["status"], t1["reason"], t1["stage"]) == ("REJECTED", "infra_unresolved", "UNRESOLVED")
    assert (t2["status"], t2["accepted_version"]) == ("ACCEPTED", 1)
    assert all(j["attempts"] <= 2 for j in p.backend.store.task_jobs("b-e2e-t02"))


cluster_only = pytest.mark.parametrize("make_pipeline", ["cluster"], indirect=True)


@cluster_only
def test_redis_losing_its_queues_mid_batch(make_pipeline):
    """Redis is only a cache of job ids; Postgres is the truth. Flush it mid-batch
    and the reconciler re-adds every claimable job."""
    import redis

    p = make_pipeline(SCENARIO)
    p.submit()
    total = len(expected_job_ids(SCENARIO))
    stack = p.start(step_s=0.15)
    p.wait_until(lambda: p.settled_count() >= total // 3)
    redis.Redis.from_url(p.env["REDIS_URL"]).flushdb()
    p.wait(stack)
    check_outcome(p)


@cluster_only
def test_s3_holds_the_durable_copy(make_pipeline):
    """Bundles, traces and the accepted task are in the bucket, not just on disk."""
    import boto3

    p = make_pipeline({"t01": SCENARIO["t01"]})
    p.submit()
    p.wait(p.start())
    check_outcome(p, {"t01": SCENARIO["t01"]}, {"t01": EXPECTED["t01"]})

    s3 = boto3.client("s3", endpoint_url=p.env["S3_ENDPOINT_URL"])
    keys = {o["Key"] for page in s3.get_paginator("list_objects_v2").paginate(Bucket=p.env["S3_BUCKET"])
            for o in page.get("Contents", [])}
    sha = p.backend.store.task("b-e2e-t01")["bundle_sha256"]
    assert f"bundles/{sha}/.complete" in keys and f"bundles/{sha}/scenario.json" in keys
    assert "accepted/fake-t01/verdict.md" in keys and "accepted/fake-t01/scenario.json" in keys
    for slot in range(1, 6):
        assert any(k.startswith(f"traces/{rollout_job_id('b-e2e-t01', 1, slot, 0)}/") for k in keys)
