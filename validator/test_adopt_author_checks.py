"""SDE_ADOPT_AUTHOR_CHECKS: validate adopts the create job's stamped sde-task runs, and
world_check the world author's stamped sde-world check, only for the exact draft / world."""

import json

import pytest

from shared.taskformat import world_check
from validator import gates

STAMP = "a" * 64
SHA = "b" * 64


def author_checks(**over):
    roles = {"oracle": {"ok": True, "reward": 1.0, "stamp": STAMP, "repeat": {"reward": 1.0, "same": True}},
             "nop": {"ok": True, "reward": 0.0, "stamp": STAMP},
             "wrong": {"ok": True, "reward": 0.0, "stamp": STAMP}}
    roles.update(over.pop("roles", {}))
    return {"job_id": "t-v1-create", "bundle_sha256": SHA, "stamp": STAMP, "roles": roles, **over}


@pytest.fixture
def adopt(monkeypatch):
    monkeypatch.setenv("SDE_ADOPT_AUTHOR_CHECKS", "1")


@pytest.fixture
def ran(monkeypatch):
    """Every gate a stub that records it ran: the Docker ones must not, when adopted."""
    calls = []

    def stub(name):
        def gate(*a, **kw):
            calls.append(kw.get("name") or (a[2] if name == "oracle" else name))
            return gates.Gate(a[2] if name == "oracle" else name, True, {"ran": True})
        return gate

    for name in ("structural", "lint", "nop", "wrong_fix", "leak"):
        monkeypatch.setattr(gates, f"gate_{name}", stub(name))
    monkeypatch.setattr(gates, "gate_oracle", stub("oracle"))
    return calls


def test_exactly_stamped_runs_are_adopted_without_docker(adopt, ran, tmp_path):
    out = gates.run_validate_job(tmp_path / "b", tmp_path / "t", author_checks=author_checks(), bundle_sha256=SHA)
    assert out["outcome"] == "OK"
    assert ran == ["structural", "lint", "leak"]  # no oracle1/oracle2/nop/wrong_fix runs
    g = out["result"]["gates"]
    for name, reward in (("oracle1", 1.0), ("oracle2", 1.0), ("nop", 0.0), ("wrong_fix", 0.0)):
        assert g[name] == {"passed": True, "reward": reward, "adopted_from": "t-v1-create", "stamp": STAMP}


@pytest.mark.parametrize("checks, sha", [
    (author_checks(), "c" * 64),                                           # a changed file: another bundle
    (author_checks(stamp=None), SHA),                                      # an old trial: no stamp
    (author_checks(roles={"nop": {"ok": True, "reward": 0.0, "stamp": "d" * 64}}), SHA),  # a stale role
    (author_checks(roles={"oracle": {"ok": True, "reward": 1.0, "stamp": STAMP,
                                     "repeat": {"reward": 1.0, "same": False}}}), SHA),    # a flaky oracle
    (author_checks(roles={"wrong": {"ok": False, "reward": 1.0, "stamp": STAMP}}), SHA),    # the wrong fix passed
    (None, SHA),                                                            # nothing recorded
], ids=["changed-bundle", "no-stamp", "stale-role", "flaky-oracle", "wrong-fix-passes", "none"])
def test_anything_less_than_an_exact_passing_stamp_runs_the_gates(adopt, ran, tmp_path, checks, sha):
    out = gates.run_validate_job(tmp_path / "b", tmp_path / "t", author_checks=checks, bundle_sha256=sha)
    assert ran == ["structural", "lint", "oracle1", "oracle2", "nop", "wrong_fix", "leak"]
    assert all("adopted_from" not in g for g in out["result"]["gates"].values())


def test_off_by_default(monkeypatch, ran, tmp_path):
    monkeypatch.delenv("SDE_ADOPT_AUTHOR_CHECKS", raising=False)
    gates.run_validate_job(tmp_path / "b", tmp_path / "t", author_checks=author_checks(), bundle_sha256=SHA)
    assert "oracle1" in ran and "nop" in ran


# --- world_check ----------------------------------------------------------------------


@pytest.fixture
def world(tmp_path):
    w = tmp_path / "world"
    for rel, text in {"environment/Dockerfile": "FROM python:3.12-slim\n", "environment/app.py": "x = 1\n",
                      "repro/repro.sh": "exit 1\n", "world_fix/fix.sh": "true\n"}.items():
        (w / rel).parent.mkdir(parents=True, exist_ok=True)
        (w / rel).write_text(text)
    (w / "world.json").write_text(json.dumps({"invariants": ["i"], "counterexample": {"sequence": ["a"]},
                                              "plausible_wrong_fix": "p"}))
    return w


def saved(world, outcome="OK"):
    return {"job_id": "t-v1-world", "stamp": world_check.world_stamp(world),
            "result": {"outcome": outcome, "failed_check": None, "feedback": "",
                       "result": {"checks": {"build": {"passed": True}}}, "stamp": world_check.world_stamp(world)}}


def test_the_world_authors_exact_check_is_adopted(adopt, world):
    author = saved(world)
    out = world_check.adopt_author_check(author, world)
    assert out["outcome"] == "OK" and out["adopted_from"] == "t-v1-world" and out["cost_usd"] == 0.0
    assert out["result"]["adopted_from"] == "t-v1-world" and out["result"]["checks"]["build"]["passed"]


def test_the_stamp_ignores_what_the_world_job_rewrites_in_world_json(world):
    before = world_check.world_stamp(world)
    meta = json.loads((world / "world.json").read_text())
    (world / "world.json").write_text(json.dumps({**meta, "spec": {"family": "f"}, "lineage": {}}, sort_keys=True))
    assert world_check.world_stamp(world) == before
    (world / "world.json").write_text(json.dumps({**meta, "invariants": ["another"]}))
    assert world_check.world_stamp(world) != before  # what the check reads is stamped


def test_a_changed_world_a_missing_stamp_a_failed_check_or_off_are_not_adopted(adopt, world, monkeypatch):
    author = saved(world)
    (world / "environment" / "app.py").write_text("x = 2\n")
    assert world_check.adopt_author_check(author, world) is None                       # a changed file
    author = saved(world)
    assert world_check.adopt_author_check({**author, "stamp": None, "result": {**author["result"], "stamp": None}},
                                          world) is None                               # an old trial
    assert world_check.adopt_author_check(saved(world, outcome="FAILED"), world) is None
    assert world_check.adopt_author_check(None, world) is None
    monkeypatch.setenv("SDE_ADOPT_AUTHOR_CHECKS", "0")
    assert world_check.adopt_author_check(saved(world), world) is None
