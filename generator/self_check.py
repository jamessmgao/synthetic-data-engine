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
missing and the check was run here.
"""

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for sub in ("validator", "authoring"):
    if str(ROOT / sub) not in sys.path:
        sys.path.insert(0, str(ROOT / sub))


def _saved(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def ensure_world_checked(world_dir: Path, artifacts_dir: Path, trace_dir: Path) -> dict:
    saved = _saved(Path(artifacts_dir) / "world_check.json")
    if saved is not None:
        ok = saved.get("outcome") == "OK"
        feedback = "" if ok else ("The world's own `sde-world check` failed and the author finished anyway:\n"
                                  + (saved.get("feedback") or ""))
        return {"ok": ok, "by": "author", "feedback": feedback, "result": saved}
    from shared.taskformat.world_check import run_world_check_job

    result = run_world_check_job(world_dir, Path(trace_dir) / "self_check")
    ok = result["outcome"] == "OK"
    feedback = "" if ok else ("The author finished without running `sde-world check`, and the check fails:\n"
                              + (result.get("feedback") or ""))
    return {"ok": ok, "by": "pipeline", "feedback": feedback, "result": result}


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
    saved = _saved(arts / "check.json")
    runs = {role: _saved(arts / f"run-{role}.json") for role in ROLES}
    check = saved
    by = "author"
    from authoring import sde_task

    with tempfile.TemporaryDirectory(prefix="sde-task-guard-") as tmp:
        if saved is None or any(r is None for r in runs.values()):
            check = sde_task.check_draft(Path(draft_path), world_dir, Path(tmp))  # also materializes the bundle
            by = "pipeline"
        if not check.get("ok"):
            why = ("failed `sde-task check` and finished anyway" if saved is not None
                   else "finished without running `sde-task check`, and it fails")
            return _not_ok(by, f"The author {why}:\n" + "\n".join(f"- {e}" for e in check.get("errors") or []),
                           check, runs)
        if any(r is None for r in runs.values()):  # never ran `sde-task run --as all`: run it once here
            fresh = (run_roles or sde_task.run_roles)(Path(check["bundle_dir"]), list(ROLES), repeat_oracle=True)
            runs = {r["as"]: r for r in fresh}
    failing = [role for role in ROLES if not (runs.get(role) or {}).get("ok")]
    if failing:
        lines = [_role_feedback(role, runs.get(role) or {}) for role in failing]
        why = "finished with failing `sde-task run` roles" if by == "author" else (
            "finished without running `sde-task run --as all`; run here, it fails")
        return _not_ok(by, f"The author {why}:\n" + "\n".join(lines), check, runs)
    return {"ok": True, "by": by, "feedback": "", "result": {"check": _public(check), "runs": _oks(runs)}}


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


def _not_ok(by: str, feedback: str, check: dict | None, runs: dict) -> dict:
    return {"ok": False, "by": by, "feedback": feedback, "result": {"check": _public(check), "runs": _oks(runs)}}
