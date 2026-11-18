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
    def __init__(self, codes=None, diff=TWO_FILES, files=None):
        self.codes, self.diff, self.calls = {**GOOD, **(codes or {})}, diff, []
        self.files = files or {}  # the fixed text of a path, as `docker exec <cid> cat <path>` returns it

    def __call__(self, args, timeout, full=False):
        self.calls.append(args)
        if args[0] == "run" and "-d" in args:
            return 0, "cid1\n"
        if args[0] == "run":
            return self.codes[args[-1]], f"ran {args[-1]}"
        if args[0] == "exec" and args[2] == "cat":
            return (0, self.files[args[3]]) if args[3] in self.files else (1, "no such file")
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


@pytest.fixture(autouse=True)
def enforce(monkeypatch):
    """Most tests here check the gate itself, so they enforce it (the default is warn)."""
    monkeypatch.setenv("SDE_WORLD_HARDNESS", "enforce")


def check(world, tmp_path, monkeypatch, fake):
    monkeypatch.setattr(wc, "_docker", fake)
    return wc.run_world_check_job(world, tmp_path / "trace")


def test_a_good_world_passes_every_check(world, tmp_path, monkeypatch):
    fake = FakeDocker()
    out = check(world, tmp_path, monkeypatch, fake)
    assert out["outcome"] == "OK", out["feedback"]
    assert out["result"]["hardness"] == {"symptom_ok": True, "spans_or_dependent": True, "wrong_fix_ok": True,
                                         "failed": [], "warnings": [], "mode": "enforce",
                                         "details": {"fix_files": ["/app/store.py", "/app/worker.py"], "via": "files"}}
    assert fake.scripts() == [REPRO, REPRO, f"{FIX} && {REPRO}", f"{FIX} && {REPRO}", SYMPTOM,
                              f"{FIX} && {SYMPTOM}", f"{WRONG} && {SYMPTOM}", f"{WRONG} && {REPRO}"]


def test_the_default_is_warn(monkeypatch):
    monkeypatch.delenv("SDE_WORLD_HARDNESS")
    assert wc.hardness_mode() == "warn"
    monkeypatch.setenv("SDE_WORLD_HARDNESS", "strict")
    with pytest.raises(ValueError):
        wc.hardness_mode()


def test_warn_records_every_hardness_failure_and_passes_the_world(world, tmp_path, monkeypatch):
    monkeypatch.setenv("SDE_WORLD_HARDNESS", "warn")
    # The symptom is invisible, the fix is one file with no dependent, the wrong fix is right.
    out = check(world, tmp_path, monkeypatch, FakeDocker({SYMPTOM: 0, f"{WRONG} && {REPRO}": 0},
                                                         diff="C /app/store.py\n"))
    assert out["outcome"] == "OK" and out["failed_check"] is None
    h = out["result"]["hardness"]
    assert (h["symptom_ok"], h["spans_or_dependent"], h["wrong_fix_ok"], h["mode"]) == (False, False, False, "warn")
    assert h["failed"] == ["symptom_visible", "fix_not_local", "wrong_fix_fails_repro"]
    saved = json.loads((tmp_path / "trace" / "world_check.json").read_text())
    assert saved["result"]["hardness"] == h


def test_warn_does_not_require_the_new_scripts(world, tmp_path, monkeypatch):
    monkeypatch.setenv("SDE_WORLD_HARDNESS", "warn")
    (world / "world_fix" / "wrong_fix.sh").unlink()
    (world / "repro" / "symptom_check.sh").unlink()
    fake = FakeDocker()
    out = check(world, tmp_path, monkeypatch, fake)
    assert out["outcome"] == "OK"
    h = out["result"]["hardness"]
    assert h["details"]["missing"] == ["repro/symptom_check.sh", "world_fix/wrong_fix.sh"]
    assert h["failed"][0] == "hardness_files" and not h["symptom_ok"] and not h["wrong_fix_ok"]
    assert not any(SYMPTOM in s or WRONG in s for s in fake.scripts())  # nothing run against missing scripts


def test_sde_world_shows_the_author_each_warning_with_a_fix_and_still_passes_in_warn(world, tmp_path, monkeypatch,
                                                                                     capsys):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "authoring"))
    from authoring import sde_world

    monkeypatch.setenv("SDE_WORLD_HARDNESS", "warn")
    monkeypatch.setenv("SDE_ARTIFACTS_DIR", str(tmp_path / "arts"))
    (tmp_path / "arts").mkdir()
    monkeypatch.setattr(wc, "_docker", FakeDocker({f"{WRONG} && {SYMPTOM}": 1}, diff="C /app/store.py\n"))
    assert sde_world.main(["check", str(world)]) == 0  # warnings don't fail it in warn mode
    err = capsys.readouterr().err
    assert "2 hardness warning(s) (mode warn: the world still passes). Fix every WARNING before finishing" in err
    assert ("WARNING: fix.sh changes 1 file(s) ['/app/store.py'] and there is no working dependent_check.sh: make "
            "the fix span 2+ modules") in err
    assert "WARNING: the plausible wrong fix does not pass symptom_check.sh, so it is not tempting" in err
    saved = json.loads((tmp_path / "arts" / "world_check.json").read_text())
    assert saved["result"]["hardness"]["failed"] == ["fix_not_local", "wrong_fix_passes_symptom"]
    assert sde_world.hardness_warnings({"failed": []}) == [] and sde_world.hardness_warnings(None) == []


def test_off_skips_the_hardness_checks(world, tmp_path, monkeypatch):
    monkeypatch.setenv("SDE_WORLD_HARDNESS", "off")
    fake = FakeDocker({SYMPTOM: 0})
    out = check(world, tmp_path, monkeypatch, fake)
    assert out["outcome"] == "OK" and "hardness" not in out["result"]
    assert fake.scripts() == [REPRO, REPRO, f"{FIX} && {REPRO}", f"{FIX} && {REPRO}"]


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


# --- limit_named: the defective code announces its own limit (aws-iter1-t02, t06) ---------

T02_ORIGINAL = '''package main

// optimize folds constants through let bindings that are never reassigned.
func optimize(e *Expr) *Expr {
    assigned := findTopLevelAssignments(e.Body)
    return substitute(e, assigned)
}

func findTopLevelAssignments(e *Expr) map[string]bool {
    assigned := make(map[string]bool)
    if e.Type == ExprBlock {
        for _, s := range e.Stmts {
            if s.Type == ExprAssign {
                assigned[s.Name] = true
            }
        }
    }
    return assigned
}
'''
T02_FIXED = (T02_ORIGINAL[:T02_ORIGINAL.index("func findTopLevelAssignments")]
             .replace("findTopLevelAssignments(e.Body)", "findAllAssignments(e.Body)")
             + "func findAllAssignments(e *Expr) map[string]bool {\n    assigned := make(map[string]bool)\n"
               "    walk(e, func(n *Expr) { if n.Type == ExprAssign { assigned[n.Name] = true } })\n"
               "    return assigned\n}\n")

T06_ORIGINAL = '''"""Recovery and consistency module for replicated KV store."""


def get_replay_target(state, commit_index):
    """Determine the highest safe index to apply during recovery.

    Only entries that have been committed (replicated and acknowledged
    by a majority) should be applied during recovery to prevent
    applying uncommitted entries from a failed leader.
    """
    return commit_index


def on_promote(state, commit_index):
    """Execute recovery when promoted to leader.

    Apply all committed entries to the state machine.
    """
    target = get_replay_target(state, commit_index)
    if target >= 0:
        state.apply_through(target)
    return target
'''
T06_FIXED = T06_ORIGINAL.replace("    return commit_index\n", "    return state.wal.last_index\n")


def test_a_fixed_function_named_for_its_limit_is_flagged():
    hits = wc._limit_named("/app/opt.go", T02_ORIGINAL, T02_FIXED)
    assert hits == [{"file": "/app/opt.go", "function": "findTopLevelAssignments", "where": "name",
                     "matched": ["toplevel"], "text": "findTopLevelAssignments"}]


def test_a_fixed_function_whose_docstring_states_its_limit_is_flagged_and_untouched_ones_are_not():
    hits = wc._limit_named("/app/recovery.py", T06_ORIGINAL, T06_FIXED)
    assert [(h["function"], h["where"], h["matched"]) for h in hits] == [
        ("get_replay_target", "comment", ["committed", "only"])]
    assert hits[0]["text"].startswith("Only entries that have been committed")
    # on_promote's docstring says "committed" too, but the fix leaves it alone
    assert wc._limit_named("/app/recovery.py", T06_ORIGINAL, T06_ORIGINAL) == []


def test_limit_words_need_whole_words_and_name_parts():
    assert "direct" not in wc._name_tokens("readDirectory") and "toplevel" in wc._name_tokens("top_level_items")
    assert wc._limit_named("/app/x.py", "def redirect_onlyish():\n    return 1\n",
                           "def redirect_onlyish():\n    return 2\n") == []


def test_limit_named_is_recorded_as_a_warning_that_never_fails_the_world(world, tmp_path, monkeypatch):
    (world / "environment" / "app" / "recovery.py").write_text(T06_ORIGINAL)
    fake = FakeDocker(diff="C /app\nC /app/recovery.py\nC /app/store.py\n",
                      files={"/app/recovery.py": T06_FIXED, "/app/store.py": "x = 2\n"})
    out = check(world, tmp_path, monkeypatch, fake)  # enforce (the autouse fixture)
    hardness = out["result"]["hardness"]
    assert out["outcome"] == "OK" and hardness["failed"] == [] and hardness["warnings"] == ["limit_named"]
    assert hardness["details"]["limit_named"][0]["function"] == "get_replay_target"


def test_sde_world_shows_the_limit_named_warning_with_the_matched_text():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "authoring"))
    from authoring import sde_world

    hit = {"file": "/app/opt.go", "function": "findTopLevelAssignments", "where": "name", "matched": ["toplevel"],
           "text": "findTopLevelAssignments"}
    lines = sde_world.hardness_warnings({"failed": [], "warnings": ["limit_named"], "details": {"limit_named": [hit]}})
    assert lines[1] == "sde-world: 1 naming warning(s) (they never fail the world):"
    assert lines[2].startswith('WARNING: /app/opt.go: findTopLevelAssignments (name: "findTopLevelAssignments") '
                               "names its own limit (toplevel), and fix.sh changes it")
