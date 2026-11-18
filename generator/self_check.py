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


def ensure_task_checked(draft_path: Path, world_dir: Path | None, artifacts_dir: Path) -> dict:
    """The deterministic part (sde-task check). The Docker runs (oracle / nop / wrong)
    are the validate job's gates; the author's saved runs are reported, not repeated."""
    saved = _saved(Path(artifacts_dir) / "sde_task" / "check.json")
    runs = {role: _saved(Path(artifacts_dir) / "sde_task" / f"run-{role}.json") for role in ("oracle", "nop", "wrong")}
    ran = {role: (r or {}).get("ok") for role, r in runs.items() if r is not None}
    if saved is not None and saved.get("ok"):
        return {"ok": True, "by": "author", "feedback": "", "result": {"check": saved, "runs": ran}}
    from authoring import sde_task

    with tempfile.TemporaryDirectory(prefix="sde-task-guard-") as tmp:
        result = sde_task.check_draft(Path(draft_path), world_dir, Path(tmp))
    result.pop("bundle_dir", None)
    ok = bool(result["ok"])
    why = "failed `sde-task check` and finished anyway" if saved is not None else "finished without running `sde-task check`"
    feedback = "" if ok else f"The author {why}:\n" + "\n".join(f"- {e}" for e in result["errors"])
    return {"ok": ok, "by": "pipeline" if saved is None else "author", "feedback": feedback,
            "result": {"check": result, "runs": ran}}
