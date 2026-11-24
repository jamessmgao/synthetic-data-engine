"""self_check.ensure_task_checked: the author's saved sde-task check, or one run here."""

import json

from generator.self_check import ensure_task_checked
from generator.test_generator import VALID, draft_with

PLAUSIBLE = "take the middle element without averaging"
WRONG = f"# plausible_wrong_fix: {PLAUSIBLE}\ncat > /app/stats.py <<'EOF'\ndef median(xs):\n    return 0\nEOF\n"


def stamp_of(draft: dict) -> str:
    """sde-task's stamp for a draft: materialize.content_sha256."""
    from shared.taskformat.materialize import content_sha256
    from shared.taskformat.schema import TaskDraft

    return content_sha256(TaskDraft.model_validate(draft))


def setup(tmp_path, draft, saved=None, roles=("oracle", "nop", "wrong"), stamp="current"):
    """The author's saved results, stamped for `draft` (`stamp`: None for an old trial, or a
    wrong hash for one that edited the draft after checking)."""
    stamp = stamp_of(draft) if stamp == "current" else stamp
    arts = tmp_path / "artifacts"
    (arts / "sde_task").mkdir(parents=True)
    if saved is not None:
        (arts / "sde_task" / "check.json").write_text(json.dumps({**saved, "stamp": stamp}))
    for role in roles:
        (arts / "sde_task" / f"run-{role}.json").write_text(json.dumps({"as": role, "ok": True, "stamp": stamp}))
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


def test_with_only_some_saved_runs_the_missing_roles_are_run_here(tmp_path):
    d, w, arts = setup(tmp_path, draft_with(wrong_fix_script=WRONG), saved={"ok": True, "errors": []},
                       roles=("nop",))
    calls = []

    def run_roles(bundle, roles, repeat_oracle=False):
        calls.append((list(roles), repeat_oracle))
        return [{"as": r, "ok": True} for r in roles]

    out = ensure_task_checked(d, w, arts, run_roles=run_roles)
    assert out["ok"] and out["by"] == "pipeline" and calls == [(["oracle", "wrong"], True)]
    stamp = out["result"]["stamp"]
    assert stamp == stamp_of(draft_with(wrong_fix_script=WRONG))
    assert {r["stamp"] for r in out["result"]["roles"].values()} == {stamp}  # fresh runs are stamped too


def test_a_saved_run_for_an_earlier_draft_is_run_again(tmp_path):
    """The author ran `sde-task run`, then edited the draft: the saved runs are for another
    draft (their stamp), so every role runs again on the final one."""
    d, w, arts = setup(tmp_path, draft_with(wrong_fix_script=WRONG), saved={"ok": True, "errors": []},
                       stamp="0" * 64)
    calls = []

    def run_roles(bundle, roles, repeat_oracle=False):
        calls.append(list(roles))
        return [{"as": r, "ok": r != "oracle", "reward": 0.0, "expected": 1.0 if r == "oracle" else 0.0}
                for r in roles]

    out = ensure_task_checked(d, w, arts, run_roles=run_roles)
    assert calls == [["oracle", "nop", "wrong"]] and not out["ok"] and out["by"] == "pipeline"
    assert "oracle: reward 0.0" in out["feedback"]


def test_a_missing_check_is_run_here_and_catches_a_broken_draft(tmp_path):
    d, w, arts = setup(tmp_path, VALID)  # no wrong fix: fails the world tie
    out = ensure_task_checked(d, w, arts)
    assert not out["ok"] and out["by"] == "pipeline"
    assert "without running `sde-task check`" in out["feedback"] and "wrong_fix_script is empty" in out["feedback"]


def test_a_failed_check_the_author_ignored_fails(tmp_path):
    d, w, arts = setup(tmp_path, VALID, saved={"ok": False, "errors": ["x"]})
    out = ensure_task_checked(d, w, arts)
    assert not out["ok"] and "finished anyway" in out["feedback"]
