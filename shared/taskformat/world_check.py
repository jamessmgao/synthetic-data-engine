"""
The `world_check` job (docs/AGENTIC_AUTHORING_PLAN.md §3.5): is a world's
counterexample real, reproducible and fixable? Deterministic, no model call.

A world (§3.3) is a directory:

    environment/         Dockerfile + the world's code, harness, fault injection
    world_fix/fix.sh     makes the invariants hold
    repro/repro.sh       deterministic reproduction: exit 1 = invariant violated, 0 = holds
    world.json           spec, invariants, counterexample, plausible wrong fix, lineage

Checks, in order, stopping at the first failure:

    structure               the files above exist; world.json has invariants and a
                            counterexample; the Dockerfile uses an allowed base image
    build                   the world's image builds (base image pinned to its digest)
    repro_fails             repro.sh exits 1 on the world, twice (the bug is real and stable)
    repro_passes_with_fix   after world_fix/fix.sh, repro.sh exits 0, twice (it is fixable)

The result is stored at <trace_dir>/world_check.json and adopted when the job is
retried. Failures carry feedback for the world repair job.
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "generator"))
from shared.contracts import config  # noqa: E402
from shared.taskformat.materialize import _pin_base_images  # noqa: E402
from shared.taskformat.schema import _check_dockerfile  # noqa: E402

RUNS = 2  # each repro check runs twice: a flaky counterexample is not a counterexample
REPRO_TIMEOUT_SEC = 600
OUTPUT_CHARS = 2000
REQUIRED = ("environment/Dockerfile", "repro/repro.sh", "world_fix/fix.sh", "world.json")


# Every world container gets the evaluation container's limits, so the check sees
# what a rollout will (1 CPU / 2 GB).
RUN_LIMITS = ["--cpus", "1", "--memory", "2g"]


def docker_label() -> list[str]:
    """Inside an authoring run, everything built or started is labeled with the job
    (SDE_DOCKER_LABEL=sde.job=<job_id>), so the harness can remove it afterwards."""
    label = os.environ.get("SDE_DOCKER_LABEL")
    return ["--label", label] if label else []


def _docker(args: list[str], timeout: float) -> tuple[int, str]:
    """Run a docker command; (exit code, combined output tail). -1 on timeout."""
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)[-OUTPUT_CHARS:]
    except subprocess.TimeoutExpired as e:
        out = ((e.stdout or b"") + (e.stderr or b"")) if isinstance(e.stdout, bytes) else (e.stdout or "")
        return -1, f"timed out after {timeout}s\n{str(out)[-OUTPUT_CHARS:]}"


def _check_structure(world: Path) -> tuple[bool, dict, str]:
    missing = [p for p in REQUIRED if not (world / p).exists()]
    if missing:
        return False, {"missing": missing}, f"The world is missing required files: {missing}."
    problems = []
    try:
        meta = json.loads((world / "world.json").read_text())
    except json.JSONDecodeError as e:
        return False, {}, f"world.json is not valid JSON: {e}"
    if not isinstance(meta.get("invariants"), list) or not meta["invariants"]:
        problems.append("world.json needs a non-empty list of invariants")
    ce = meta.get("counterexample") or {}
    if not isinstance(ce.get("sequence"), list) or not ce["sequence"]:
        problems.append("world.json counterexample needs the event sequence that violates an invariant")
    env = world / "environment"
    env_paths = [str(p.relative_to(env)) for p in env.rglob("*") if p.is_file() and p.name != "Dockerfile"]
    problems += _check_dockerfile((env / "Dockerfile").read_text(), env_paths)
    if problems:
        return False, {"problems": problems}, "The world's structure is invalid:\n" + "\n".join(f"- {p}" for p in problems)
    return True, {"invariants": len(meta["invariants"])}, ""


def _image_tag(world: Path) -> str:
    """Unique per check: two checks of the same world (a reclaimed job and its
    original, or a retry) must not `rmi -f` each other's image mid-run."""
    return f"sde-world-{world.name[:16].lower()}-{uuid.uuid4().hex[:8]}"


# The check scripts are baked into an image derived from the world's, not
# bind-mounted: the check also runs inside the authoring container through the
# host's Docker socket, where `-v <path>` would name a path that doesn't exist on
# the host. A build uploads its context from the client, so it works anywhere.
CHECK_DOCKERFILE = "FROM {image}\nCOPY repro /world/repro\nCOPY world_fix /world/world_fix\n"


def _build_check_image(image: str, world: Path, trace_dir: Path) -> tuple[str, int, str]:
    dockerfile = trace_dir / "Dockerfile.check"
    dockerfile.write_text(CHECK_DOCKERFILE.format(image=image))
    check = f"{image}-check"
    code, out = _docker(["build", "-q", *docker_label(), "-t", check, "-f", str(dockerfile), str(world)],
                        config.BUILD_TIMEOUT_SEC)
    return check, code, out


def _repro(check_image: str, with_fix: bool) -> tuple[int, str]:
    script = "bash /world/world_fix/fix.sh && bash /world/repro/repro.sh" if with_fix else "bash /world/repro/repro.sh"
    return _docker(["run", "--rm", *RUN_LIMITS, *docker_label(), check_image, "bash", "-c", script],
                   REPRO_TIMEOUT_SEC)


def run_world_check_job(world_dir: str | Path, trace_dir: str | Path) -> dict:
    """Check one world. Returns {outcome, failed_check, result: {checks}, feedback, cost_usd}."""
    world, trace_dir = Path(world_dir), Path(trace_dir)
    record = trace_dir / "world_check.json"
    if record.exists():
        return {**json.loads(record.read_text()), "adopted": True}

    checks: dict[str, dict] = {}

    def finish(failed: str | None, feedback: str = "") -> dict:
        out = {"outcome": "FAILED" if failed else "OK", "failed_check": failed, "result": {"checks": checks},
               "feedback": feedback, "cost_usd": 0.0}
        trace_dir.mkdir(parents=True, exist_ok=True)
        record.write_text(json.dumps(out, indent=1) + "\n")
        return out

    ok, detail, feedback = _check_structure(world)
    checks["structure"] = {"passed": ok, **detail}
    if not ok:
        return finish("structure", feedback)

    trace_dir.mkdir(parents=True, exist_ok=True)
    pinned = trace_dir / "Dockerfile.pinned"
    pinned.write_text(_pin_base_images((world / "environment" / "Dockerfile").read_text()))
    image = _image_tag(world)
    code, out = _docker(["build", "-q", *docker_label(), "-t", image, "-f", str(pinned), str(world / "environment")],
                        config.BUILD_TIMEOUT_SEC)
    checks["build"] = {"passed": code == 0, "exit_code": code, "output": out if code else ""}
    if code != 0:
        return finish("build", f"The world's image does not build (exit {code}):\n{out}")

    check_image = f"{image}-check"
    try:
        check_image, code, out = _build_check_image(image, world, trace_dir)
        if code != 0:
            checks["build"] = {**checks["build"], "passed": False, "exit_code": code, "output": out}
            return finish("build", f"The world's check image does not build (exit {code}):\n{out}")
        for name, with_fix, want in (("repro_fails", False, 1), ("repro_passes_with_fix", True, 0)):
            runs = [_repro(check_image, with_fix) for _ in range(RUNS)]
            passed = all(c == want for c, _ in runs)
            checks[name] = {"passed": passed, "exit_codes": [c for c, _ in runs],
                            "output": "" if passed else runs[-1][1]}
            if not passed:
                if with_fix:
                    msg = ("After world_fix/fix.sh, repro/repro.sh must exit 0 (the invariants hold) every run; "
                           f"it exited {[c for c, _ in runs]}.")
                else:
                    msg = ("On the unfixed world, repro/repro.sh must exit 1 (an invariant is violated) every run; "
                           f"it exited {[c for c, _ in runs]}. The counterexample must be real and deterministic "
                           "(exit 0 = it did not reproduce, other codes = the script itself failed).")
                return finish(name, f"{msg}\nOutput of the last run:\n{runs[-1][1]}")
        return finish(None)
    finally:
        _docker(["rmi", "-f", check_image, image], 120)
