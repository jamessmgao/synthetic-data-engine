"""self_check.ensure_task_checked: the author's saved sde-task check, or one run here."""

import json

from generator.self_check import ensure_task_checked
from test_generator import VALID, draft_with

PLAUSIBLE = "take the middle element without averaging"
WRONG = f"# plausible_wrong_fix: {PLAUSIBLE}\ncat > /app/stats.py <<'EOF'\ndef median(xs):\n    return 0\nEOF\n"


def setup(tmp_path, draft, saved=None, roles=("oracle", "nop", "wrong")):
    arts = tmp_path / "artifacts"
    (arts / "sde_task").mkdir(parents=True)
    if saved is not None:
        (arts / "sde_task" / "check.json").write_text(json.dumps(saved))
    for role in roles:
        (arts / "sde_task" / f"run-{role}.json").write_text(json.dumps({"as": role, "ok": True}))
    d = tmp_path / "draft.json"
    d.write_text(json.dumps(draft))
    w = tmp_path / "world"
    w.mkdir()
    (w / "world.json").write_text(json.dumps({"plausible_wrong_fix": PLAUSIBLE}))
    return d, w, arts


def test_the_authors_passing_check_is_used(tmp_path):
    d, w, arts = setup(tmp_path, draft_with(wrong_fix_script=WRONG), saved={"ok": True, "errors": []})
    out = ensure_task_checked(d, w, arts)
    assert out["ok"] and out["by"] == "author"
    assert out["result"]["runs"] == {"oracle": True, "nop": True, "wrong": True}


def test_with_only_some_saved_runs_every_role_is_run_here(tmp_path):
    d, w, arts = setup(tmp_path, draft_with(wrong_fix_script=WRONG), saved={"ok": True, "errors": []},
                       roles=("nop",))
    calls = []

    def run_roles(bundle, roles, repeat_oracle=False):
        calls.append(list(roles))
        return [{"as": r, "ok": True} for r in roles]

    out = ensure_task_checked(d, w, arts, run_roles=run_roles)
    assert out["ok"] and out["by"] == "pipeline" and calls == [["oracle", "nop", "wrong"]]


def test_a_missing_check_is_run_here_and_catches_a_broken_draft(tmp_path):
    d, w, arts = setup(tmp_path, VALID)  # no wrong fix: fails the world tie
    out = ensure_task_checked(d, w, arts)
    assert not out["ok"] and out["by"] == "pipeline"
    assert "without running `sde-task check`" in out["feedback"] and "wrong_fix_script is empty" in out["feedback"]


def test_a_failed_check_the_author_ignored_fails(tmp_path):
    d, w, arts = setup(tmp_path, VALID, saved={"ok": False, "errors": ["x"]})
    out = ensure_task_checked(d, w, arts)
    assert not out["ok"] and "finished anyway" in out["feedback"]
