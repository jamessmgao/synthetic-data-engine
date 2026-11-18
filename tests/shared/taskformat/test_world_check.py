"""shared/taskformat/world_check.py's checks, against a scripted Docker (no builds, no containers)."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared.taskformat import world_check as wc  # noqa: E402

FIX, WRONG, REPRO, SYMPTOM, DEPENDENT = wc.FIX, wc.WRONG, wc.REPRO, wc.SYMPTOM, wc.DEPENDENT

# A good world: the bug is real, the symptom visible, the fix spans two files, and the
# wrong fix hides the symptom but not the deeper schedule.
GOOD = {REPRO: 1, SYMPTOM: 1, f"{FIX} && {REPRO}": 0, f"{FIX} && {SYMPTOM}": 0,
        f"{WRONG} && {SYMPTOM}": 0, f"{WRONG} && {REPRO}": 1,
        DEPENDENT: 0, f"{FIX} && {DEPENDENT}": 0, f"{WRONG} && {DEPENDENT}": 1}
TWO_FILES = "C /app\nC /app/store.py\nC /app/worker.py\nA /tmp/x\nA /app/__pycache__/store.cpython-312.pyc\n"


class FakeDocker:
    def __init__(self, codes=None, diff=TWO_FILES):
        self.codes, self.diff, self.calls = {**GOOD, **(codes or {})}, diff, []

    def __call__(self, args, timeout):
        self.calls.append(args)
        if args[0] == "run" and "-d" in args:
            return 0, "cid1\n"
        if args[0] == "run":
            return self.codes[args[-1]], f"ran {args[-1]}"
        if args[0] == "exec":
            return 0, ""
        if args[0] == "diff":
            return 0, self.diff
        return 0, ""

    def scripts(self):
        return [c[-1] for c in self.calls if c[0] == "run" and "-d" not in c]


@pytest.fixture
def world(tmp_path):
    w = tmp_path / "world"
    for rel, text in {"environment/Dockerfile": "FROM python:3.12-slim-bookworm\nWORKDIR /app\nCOPY app/ /app/\n",
                      "environment/app/store.py": "x = 1\n", "repro/repro.sh": "exit 1\n",
                      "repro/symptom_check.sh": "exit 1\n", "world_fix/fix.sh": "true\n",
                      "world_fix/wrong_fix.sh": "true\n"}.items():
        (w / rel).parent.mkdir(parents=True, exist_ok=True)
        (w / rel).write_text(text)
    (w / "world.json").write_text(json.dumps({"invariants": ["no double debit"],
                                              "counterexample": {"sequence": ["a", "b"]},
                                              "plausible_wrong_fix": "an in-memory set"}))
    return w


def check(world, tmp_path, monkeypatch, fake):
    monkeypatch.setattr(wc, "_docker", fake)
    return wc.run_world_check_job(world, tmp_path / "trace")


def test_a_good_world_passes_every_check(world, tmp_path, monkeypatch):
    fake = FakeDocker()
    out = check(world, tmp_path, monkeypatch, fake)
    assert out["outcome"] == "OK", out["feedback"]
    assert out["result"]["checks"]["fix_not_local"] == {"passed": True, "fix_files": ["/app/store.py", "/app/worker.py"]}
    assert fake.scripts() == [REPRO, REPRO, SYMPTOM, f"{FIX} && {REPRO}", f"{FIX} && {REPRO}",
                              f"{FIX} && {SYMPTOM}", f"{WRONG} && {SYMPTOM}", f"{WRONG} && {REPRO}"]


@pytest.mark.parametrize("codes,failed,says", [
    ({REPRO: 0}, "repro_fails", "must exit 1"),
    ({SYMPTOM: 0}, "symptom_visible", "symptom users report"),
    ({f"{FIX} && {SYMPTOM}": 1}, "symptom_gone_with_fix", "symptom_check.sh must exit 0"),
    # The wrong fix doesn't even hide the symptom: an agent's first check would reject it.
    ({f"{WRONG} && {SYMPTOM}": 1}, "wrong_fix_passes_symptom", "makes the visible symptom go away"),
    # The wrong fix is actually right: nothing deeper breaks it.
    ({f"{WRONG} && {REPRO}": 0}, "wrong_fix_fails_repro", "still violates an invariant"),
])
def test_each_check_fails_with_feedback(world, tmp_path, monkeypatch, codes, failed, says):
    out = check(world, tmp_path, monkeypatch, FakeDocker(codes))
    assert out["outcome"] == "FAILED" and out["failed_check"] == failed and says in out["feedback"]


def test_a_one_file_fix_needs_a_dependent(world, tmp_path, monkeypatch):
    one_file = "C /app\nC /app/store.py\n"
    out = check(world, tmp_path, monkeypatch, FakeDocker(diff=one_file))
    assert out["failed_check"] == "fix_not_local" and "dependent_check.sh" in out["feedback"]
    assert "['/app/store.py']" in out["feedback"]


def test_a_one_file_fix_with_a_dependent_that_the_local_patch_breaks_passes(world, tmp_path, monkeypatch):
    (world / "repro" / "dependent_check.sh").write_text("exit 0\n")
    fake = FakeDocker(diff="C /app/store.py\n")
    out = check(world, tmp_path, monkeypatch, fake)
    assert out["outcome"] == "OK", out["feedback"]
    assert {DEPENDENT, f"{FIX} && {DEPENDENT}", f"{WRONG} && {DEPENDENT}"} <= set(fake.scripts())


@pytest.mark.parametrize("codes,failed", [
    ({DEPENDENT: 1}, "dependent_holds"),
    ({f"{FIX} && {DEPENDENT}": 1}, "dependent_holds_with_fix"),
    ({f"{WRONG} && {DEPENDENT}": 0}, "dependent_breaks_with_wrong_fix"),
])
def test_a_dependent_that_does_not_discriminate_fails(world, tmp_path, monkeypatch, codes, failed):
    (world / "repro" / "dependent_check.sh").write_text("exit 0\n")
    out = check(world, tmp_path, monkeypatch, FakeDocker(codes, diff="C /app/store.py\n"))
    assert out["failed_check"] == failed


def test_the_new_scripts_are_required(world, tmp_path, monkeypatch):
    (world / "world_fix" / "wrong_fix.sh").unlink()
    (world / "repro" / "symptom_check.sh").unlink()
    out = check(world, tmp_path, monkeypatch, FakeDocker())
    assert out["failed_check"] == "structure"
    assert "repro/symptom_check.sh" in out["feedback"] and "world_fix/wrong_fix.sh" in out["feedback"]
