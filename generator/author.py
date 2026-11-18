"""
The agent authoring jobs (docs/AGENTIC_AUTHORING_PLAN.md §3.3-3.4, §3.6):

    run_world_job(job_id, job_input, work_dir=None, *, run_trial=None) -> {
        outcome: "OK" | "FAILED", error, cost_usd,
        result: {world_sha256, world_path, name, authoring: {...},
                 "failure_kind": "infra" | "author"  (FAILED only), "feedback" (FAILED author only)}}
    run_create_job(...)   from author_task (TB3), re-exported here

A world job runs one authoring trial (authoring_harness) whose agent builds,
repairs or mutates a world and writes it to /logs/artifacts/world/. This module
renders the instruction, hands the inputs over, then checks the output's
structure, sets world.json's spec and lineage, and stores the world
content-addressed at <work>/worlds/<world_sha256>/ (the pipeline registers it).

Origins:
    initial    {origin, spec}: a new world from the spec (prompts/world.md)
    repair     {origin, world_sha256, feedback, spec}: the world failed world_check;
               fix it. Its lineage is carried over unchanged.
    mutation   {origin, parent_world_sha256, spec, mutation, mutations, analysis,
               target_fix}: make the passing strategy fail (prompts/world_mutation.md);
               lineage = {parent, mutations from the input}.

Failure kinds: "infra" (Harbor or the agent run itself broke: no trial, an exception,
no artifacts) vs "author" (the agent ran but left no usable world).
"""

import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from string import Template
from typing import Any, Callable

from shared.contracts import config
from generator.author_task import render_world_mutation_instruction, run_create_job  # noqa: F401  (re-export)
from generator.families import brief_for
from shared.taskformat.schema import BASE_IMAGES
from generator.self_check import ensure_world_checked

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "validator"))
from shared.taskformat.world_check import _check_structure  # noqa: E402

PROMPTS = Path(config.PROMPTS_DIR)
WORLD_OUT = "world"  # under the trial's artifacts dir (/logs/artifacts/world/ in the container)
# A safety net for a hung session only (2 h): the prompt suggests ~12 min, it doesn't enforce it.
AUTHOR_TIMEOUT_SEC = float(os.environ.get("SDE_WORLD_TIMEOUT_S", "7200"))
MAX_WORLD_BYTES = 50_000_000
MAX_WORLD_FILES = 5_000

REPAIR_SECTION = """# This is a REPAIR

The world you are given failed the pipeline's world check. It is in `/input/world/`
(read-only; copy it to `/work/world` to edit). Fix what the check found, keeping its
invariants and counterexample unless the check shows they are wrong, and write the
repaired world to `/logs/artifacts/world/` in the same layout.

What the check reported:

```
$feedback
```"""

INITIAL_SECTION = "# This is a NEW world\n\nNothing exists yet: build it from the spec below."


def tree_sha256(root: Path) -> str:
    """Hash of every file's relative path, executable bit and bytes, in sorted order
    (the same scheme as task bundles)."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        h.update(f"{rel}\0{int(os.access(path, os.X_OK))}\0".encode())
        h.update(hashlib.sha256(path.read_bytes()).digest())
    return h.hexdigest()


def _files_under(prefix: str, root: Path) -> dict[str, bytes]:
    return {f"{prefix}/{p.relative_to(root).as_posix()}": p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def render_world_instruction(spec: dict, origin: str = "initial", feedback: str = "") -> str:
    section = INITIAL_SECTION if origin == "initial" else Template(REPAIR_SECTION).safe_substitute(
        feedback=(feedback or "(no feedback recorded)")[-4000:])
    return Template((PROMPTS / "world.md").read_text()).safe_substitute(
        origin_section=section,
        spec_json=json.dumps(spec, indent=1, sort_keys=True),
        family_brief=brief_for(spec.get("family")),
        base_images=", ".join(f"`{i}`" for i in sorted(BASE_IMAGES)),
    ).rstrip() + "\n"


def _authoring(trial: dict | None) -> dict | None:
    if trial is None:
        return None
    keys = ("trial_dir", "steps", "duration_sec", "n_input_tokens", "n_output_tokens", "gateway_cost_usd",
            "exception_type", "adopted", "instruction_size")
    return {k: trial.get(k) for k in keys}


def _failed(kind: str, error: str, trial: dict | None, feedback: str | None = None) -> dict:
    result: dict[str, Any] = {"failure_kind": kind, "world_sha256": None, "authoring": _authoring(trial)}
    if feedback:
        result["feedback"] = feedback
    return {"outcome": "FAILED", "error": error, "cost_usd": (trial or {}).get("cost_usd") or 0.0,
            "result": result}


def _load_world(work: Path, sha: str | None) -> tuple[Path | None, dict]:
    if not sha:
        return None, {}
    d = work / "worlds" / sha
    meta = json.loads((d / "world.json").read_text()) if (d / "world.json").is_file() else {}
    return d, meta


def _store(world: Path, work: Path) -> tuple[str, Path]:
    """Content-address `world` into <work>/worlds/<sha>/ (staged, then renamed)."""
    sha = tree_sha256(world)
    dest = work / "worlds" / sha
    if not dest.exists():
        stage = dest.parent / ".staging" / f"{sha}.{uuid.uuid4().hex}"
        stage.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(world, stage)
        try:
            stage.rename(dest)
        except OSError:  # another process stored the same world first
            shutil.rmtree(stage, ignore_errors=True)
    return sha, dest


def run_world_job(job_id: str, job_input: dict, work_dir: str | Path | None = None, *,
                  run_trial: Callable[..., dict] | None = None) -> dict:
    """Build, repair or mutate a world; see the module docstring."""
    if run_trial is None:
        from generator.authoring_harness import run_authoring_trial as run_trial
    work = Path(work_dir or config.WORK_DIR)
    origin = job_input.get("origin") or "initial"
    spec = dict(job_input.get("spec") or {})

    if origin == "mutation":
        parent_dir, parent = _load_world(work, job_input.get("parent_world_sha256"))
        if parent_dir is None or not parent:
            return _failed("infra", "the parent world is not available locally", None)
        spec = spec or parent.get("spec") or {}
        try:
            instruction = render_world_mutation_instruction(job_input, parent, "/input/parent",
                                                            f"/logs/artifacts/{WORLD_OUT}")
        except ValueError as e:  # missing or unknown operators: a bad input, not worth retrying
            return _failed("infra", f"bad mutation input: {e}", None)
        files = _files_under("parent", parent_dir)
        lineage = {"parent_world_sha256": job_input["parent_world_sha256"],
                   "mutations": list(job_input.get("mutations") or [])}
    elif origin == "repair":
        world_dir, world = _load_world(work, job_input.get("world_sha256"))
        if world_dir is None or not world:
            return _failed("infra", "the world to repair is not available locally", None)
        spec = spec or world.get("spec") or {}
        instruction = render_world_instruction(spec, "repair", job_input.get("feedback") or "")
        files = _files_under("world", world_dir)
        lineage = world.get("lineage") or {"parent_world_sha256": None, "mutations": []}
    elif origin == "initial":
        instruction = render_world_instruction(spec)
        files = {}
        lineage = {"parent_world_sha256": None, "mutations": []}
    else:
        raise ValueError(f"unknown world origin {origin!r}")

    trial = run_trial(job_id, instruction, files, work, timeout_sec=AUTHOR_TIMEOUT_SEC)
    if trial.get("artifacts_dir") is None:
        return _failed("infra", trial.get("error") or "the authoring run left no trial", trial)
    out = Path(trial["artifacts_dir"]) / WORLD_OUT
    if not out.is_dir():
        kind = "infra" if trial.get("exception_type") else "author"
        return _failed(kind, trial.get("error") or "the agent wrote no world to /logs/artifacts/world/", trial,
                       "No world was written to /logs/artifacts/world/.")

    files_out = [p for p in out.rglob("*") if p.is_file()]
    size = sum(p.stat().st_size for p in files_out)
    if len(files_out) > MAX_WORLD_FILES or size > MAX_WORLD_BYTES:
        msg = (f"The world is too large ({len(files_out)} files, {size} bytes; the limits are {MAX_WORLD_FILES} "
               f"files and {MAX_WORLD_BYTES} bytes). Keep build outputs and data out of it.")
        return _failed("author", msg, trial, msg)
    ok, _, feedback = _check_structure(out)
    if not ok:
        return _failed("author", feedback, trial, feedback)
    guard = ensure_world_checked(out, Path(trial["artifacts_dir"]), work / "traces" / job_id)
    if not guard["ok"]:
        return _failed("author", guard["feedback"], trial, guard["feedback"])

    meta = json.loads((out / "world.json").read_text())
    meta["spec"] = spec
    if origin == "mutation":
        # The author records what the mutation defeats (world_mutation.md); keep that on
        # this mutation's entry, the rest of the history comes from the job input.
        written = ((meta.get("lineage") or {}).get("mutations") or [{}])[-1]
        if lineage["mutations"] and written.get("defeats") and "defeats" not in lineage["mutations"][-1]:
            lineage["mutations"][-1] = {**lineage["mutations"][-1], "defeats": written["defeats"]}
    meta["lineage"] = lineage
    (out / "world.json").write_text(json.dumps(meta, indent=1, sort_keys=True) + "\n")
    sha, dest = _store(out, work)
    name = meta.get("name") or "-".join(filter(None, (spec.get("family"), spec.get("mechanism")))) or "world"
    authoring = _authoring(trial)
    authoring["self_check"] = {"by": guard["by"], "outcome": guard["result"].get("outcome")}
    return {"outcome": "OK", "error": None, "cost_usd": trial.get("cost_usd") or 0.0,
            "result": {"world_sha256": sha, "world_path": str(dest), "name": name, "authoring": authoring}}
