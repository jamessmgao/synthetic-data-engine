"""
End-of-trial guards: did the authoring agent self-check before it finished?

sde-world and sde-task save their results under the trial's /logs/artifacts/
(world_check.json; sde_task/check.json, sde_task/run-<role>.json). An agent that ends
without them, or with a failing one, made a world or draft that would fail later
anyway; checking once here fails it fast and cheaply (Docker time, no model call)
instead of after a world_check job or a validate job has been queued and run.

    ensure_world_checked(world_dir, artifacts_dir, trace_dir) -> {ok, by, feedback, result}
    ensure_task_checked(draft_path, world_dir, artifacts_dir) -> {ok, by, feedback, result}

`by` is "author" when the agent's own saved check is used, "pipeline" when it was
missing or stale and the check was run here. A saved result counts only if its `stamp`
(sde-task: materialize.content_sha256 of the draft; sde-world: world_check.world_stamp)
matches the final draft or world: an author who edited after checking, or an old trial
without stamps, is checked here again. The returned result carries the stamp, so a
validate or world_check job can adopt it (SDE_ADOPT_AUTHOR_CHECKS).
"""

import json
import tempfile
from pathlib import Path


def _saved(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def ensure_world_checked(world_dir: Path, artifacts_dir: Path, trace_dir: Path) -> dict:
    from shared.taskformat.world_check import run_world_check_job, world_stamp

    stamp = world_stamp(world_dir)
    saved = _saved(Path(artifacts_dir) / "world_check.json")
    if saved is not None and saved.get("stamp") == stamp:
        ok = saved.get("outcome") == "OK"
        feedback = "" if ok else ("The world's own `sde-world check` failed and the author finished anyway:\n"
                                  + (saved.get("feedback") or ""))
        return {"ok": ok, "by": "author", "feedback": feedback, "result": saved, "stamp": stamp}

    result = {**run_world_check_job(world_dir, Path(trace_dir) / "self_check"), "stamp": stamp}
    ok = result["outcome"] == "OK"
    why = ("ran `sde-world check` on an earlier version of the world" if saved is not None
           else "finished without running `sde-world check`")
    feedback = "" if ok else f"The author {why}, and the check fails:\n" + (result.get("feedback") or "")
    return {"ok": ok, "by": "pipeline", "feedback": feedback, "result": result, "stamp": stamp}


ROLES = ("oracle", "nop", "wrong")
TAIL_CHARS = 600


def ensure_task_checked(draft_path: Path, world_dir: Path | None, artifacts_dir: Path,
                        run_roles=None) -> dict:
    """The author's self-check, enforced: `sde-task check` must be ok AND every role's
    `sde-task run` (oracle reward 1, nop 0, wrong 0) must be ok. The author's saved results
    are used; a missing check is run here (free), and missing runs are run here once,
    all three roles on one build (`run_roles`: sde_task.run_roles, a fake in tests).
    Not ok: the feedback names each failing role's tests (local-agent-b2-t01 finished with
    its own oracle failing and was let through to validate)."""
    arts = Path(artifacts_dir) / "sde_task"
    from authoring import sde_task

    stamp = _draft_stamp(Path(draft_path))
    current = lambda saved: saved if saved is not None and stamp and saved.get("stamp") == stamp else None  # noqa: E731
    saved = current(_saved(arts / "check.json"))
    runs = {role: current(_saved(arts / f"run-{role}.json")) for role in ROLES}
    stale = [role for role in ROLES if runs[role] is None and _saved(arts / f"run-{role}.json") is not None]
    check = saved
    by = "author"

    with tempfile.TemporaryDirectory(prefix="sde-task-guard-") as tmp:
        if saved is None or any(r is None for r in runs.values()):
            check = sde_task.check_draft(Path(draft_path), world_dir, Path(tmp))  # also materializes the bundle
            by = "pipeline"
        if not check.get("ok"):
            why = ("failed `sde-task check` and finished anyway" if saved is not None
                   else "finished without running `sde-task check` on the final draft, and it fails")
            return _not_ok(by, f"The author {why}:\n" + "\n".join(f"- {e}" for e in check.get("errors") or []),
                           check, runs, stamp)
        if missing := [role for role in ROLES if runs[role] is None]:
            # never run on the final draft (or not at all): run those roles once here, on one build
            fresh = (run_roles or sde_task.run_roles)(Path(check["bundle_dir"]), missing,
                                                     repeat_oracle="oracle" in missing)
            runs.update({r["as"]: {**r, "stamp": stamp} for r in fresh})
    failing = [role for role in ROLES if not (runs.get(role) or {}).get("ok")]
    if failing:
        lines = [_role_feedback(role, runs.get(role) or {}) for role in failing]
        why = "finished with failing `sde-task run` roles" if by == "author" else (
            f"changed the draft after `sde-task run` ({', '.join(stale)}); run here on the final draft, it fails"
            if stale else "finished without running `sde-task run --as all`; run here, it fails")
        return _not_ok(by, f"The author {why}:\n" + "\n".join(lines), check, runs, stamp)
    return {"ok": True, "by": by, "feedback": "",
            "result": {"check": _public(check), "runs": _oks(runs), "stamp": stamp, "roles": _roles(runs)}}


def _draft_stamp(draft_path: Path) -> str | None:
    """materialize.content_sha256 of the final draft; None if it isn't a valid TaskDraft
    (then nothing saved counts, and sde_task.check_draft reports why)."""
    from shared.taskformat.materialize import content_sha256
    from shared.taskformat.schema import TaskDraft

    try:
        return content_sha256(TaskDraft.model_validate_json(Path(draft_path).read_text()))
    except Exception:  # unreadable or invalid: check_draft gives the author the errors
        return None


def _roles(runs: dict) -> dict:
    """What a validate job needs to adopt the runs: per role ok and reward, and the oracle's
    repeat (the flake check: oracle1 and oracle2)."""
    out = {}
    for role, r in runs.items():
        if r is not None:
            out[role] = {"ok": bool(r.get("ok")), "reward": r.get("reward"), "stamp": r.get("stamp"),
                         **({"repeat": r["repeat"]} if r.get("repeat") is not None else {})}
    return out


def _role_feedback(role: str, run: dict) -> str:
    tests = run.get("tests") or []
    failing = [t["name"] for t in tests if t.get("status") not in ("passed", "skipped")]
    parts = [f"- {role}: reward {run.get('reward')} (expected {run.get('expected')})"]
    if failing:
        parts.append(f"failing tests: {', '.join(failing[:10])}")
    if run.get("setup_errors"):
        parts.append(f"setup errors: {'; '.join(run['setup_errors'])}")
    if not run:
        parts = [f"- {role}: no result"]
    out = "; ".join(parts)
    if tail := (run.get("output_tail") or "")[-TAIL_CHARS:]:
        out += "\n  output tail:\n" + "\n".join(f"    {line}" for line in tail.splitlines())
    return out


def _oks(runs: dict) -> dict:
    return {role: (r or {}).get("ok") for role, r in runs.items() if r is not None}


def _public(check: dict | None) -> dict:
    return {k: v for k, v in (check or {}).items() if k != "bundle_dir"}


def _not_ok(by: str, feedback: str, check: dict | None, runs: dict, stamp: str | None = None) -> dict:
    return {"ok": False, "by": by, "feedback": feedback,
            "result": {"check": _public(check), "runs": _oks(runs), "stamp": stamp}}
