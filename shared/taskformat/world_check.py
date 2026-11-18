"""
The `world_check` job (docs/AGENTIC_AUTHORING_PLAN.md §3.5): is a world's
counterexample real, reproducible and fixable? Deterministic, no model call.

A world (§3.3) is a directory:

    environment/              Dockerfile + the world's code, harness, fault injection
    world_fix/fix.sh          makes the invariants hold
    world_fix/wrong_fix.sh    the plausible wrong fix, implemented
    repro/repro.sh            deterministic reproduction: exit 1 = invariant violated, 0 = holds
    repro/symptom_check.sh    the instruction-visible symptom (one simple replay of the
                              failure an agent would try first): exit 1 = seen, 0 = gone
    repro/dependent_check.sh  optional: a dependent elsewhere that a one-file fix breaks
    world.json                spec, invariants, counterexample, plausible wrong fix, lineage

Checks, in order, stopping at the first failure:

    structure               the files above exist; world.json has invariants and a
                            counterexample; the Dockerfile uses an allowed base image
    build                   the world's image builds (base image pinned to its digest)
    repro_fails             repro.sh exits 1 on the world, twice (the bug is real and stable)
    repro_passes_with_fix   after world_fix/fix.sh, repro.sh exits 0, twice (it is fixable)

The hardness checks then all run (SDE_WORLD_HARDNESS: enforce = the first failing one
fails the world; warn, the default = recorded in result.hardness {symptom_ok,
spans_or_dependent, wrong_fix_ok, failed, details, mode}, the world passes; off =
skipped). symptom_check.sh and wrong_fix.sh are required files only when enforced.

    symptom_visible         symptom_check.sh exits 1 on the world (an agent can see it),
    symptom_gone_with_fix   and 0 after fix.sh
    fix_not_local           the fix can't be one local patch: fix.sh changes 2+ files, or
                            dependent_check.sh exits 0 on the world and after fix.sh but 1
                            after wrong_fix.sh (a one-file patch breaks a dependent: the f1
                            pattern, docs/ANALYSIS.md)
    wrong_fix_survives      after wrong_fix.sh, symptom_check.sh exits 0 (it passes the
                            agent's own check) and repro.sh still exits 1 (deeper schedules
                            break it)

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
HARDNESS_FILES = ("repro/symptom_check.sh", "world_fix/wrong_fix.sh")  # required only when enforced
HARDNESS_MODES = ("enforce", "warn", "off")


def hardness_mode() -> str:
    """SDE_WORLD_HARDNESS: enforce (a world failing a hardness check fails), warn (they run and
    are recorded in result.hardness, the world passes: the default, to measure first), off."""
    mode = os.environ.get("SDE_WORLD_HARDNESS", "warn").strip().lower() or "warn"
    if mode not in HARDNESS_MODES:
        raise ValueError(f"SDE_WORLD_HARDNESS must be one of {HARDNESS_MODES}, not {mode!r}")
    return mode


def required_files() -> tuple[str, ...]:
    return REQUIRED + (HARDNESS_FILES if hardness_mode() == "enforce" else ())
MIN_FIX_FILES = 2
# Paths a fix may touch without it counting as an edit of the system.
_SCRATCH = ("/tmp/", "/var/", "/root/.cache/", "/run/", "/proc/", "/dev/", "/world/", "/logs/")


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
    missing = [p for p in required_files() if not (world / p).exists()]
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


FIX, WRONG = "bash /world/world_fix/fix.sh", "bash /world/world_fix/wrong_fix.sh"
REPRO, SYMPTOM, DEPENDENT = ("bash /world/repro/repro.sh", "bash /world/repro/symptom_check.sh",
                             "bash /world/repro/dependent_check.sh")


def _run(check_image: str, script: str) -> tuple[int, str]:
    """One fresh container from the world's check image: run `script` from its WORKDIR."""
    return _docker(["run", "--rm", *RUN_LIMITS, *docker_label(), check_image, "bash", "-c", script],
                   REPRO_TIMEOUT_SEC)


def _repro(check_image: str, with_fix: bool) -> tuple[int, str]:
    return _run(check_image, f"{FIX} && {REPRO}" if with_fix else REPRO)


def _changed_files(check_image: str, script: str) -> tuple[list[str] | None, str]:
    """The system files `script` changes (docker diff of a container after it), scratch and
    the check's own files left out; directories only listed for their contents are dropped.
    None if the script fails."""
    code, out = _docker(["run", "-d", *RUN_LIMITS, *docker_label(), "--entrypoint", "sleep", check_image,
                         "infinity"], 120)
    if code != 0:
        return None, out
    cid = out.strip().splitlines()[-1]
    try:
        code, out = _docker(["exec", cid, "bash", "-c", script], REPRO_TIMEOUT_SEC)
        if code != 0:
            return None, out
        code, diff = _docker(["diff", cid], 120)
        paths = sorted({line[2:].strip() for line in diff.splitlines() if line[:2] in ("A ", "C ", "D ")})
        files = [p for p in paths
                 if not any(other.startswith(p.rstrip("/") + "/") for other in paths)  # a directory of others
                 and not (p + "/").startswith(_SCRATCH) and "__pycache__" not in p and not p.endswith(".pyc")]
        return files, ""
    finally:
        _docker(["rm", "-f", cid], 120)


def _hardness(world: Path, check_image: str, expect) -> tuple[dict, list[tuple[str, str]]]:
    """Run every hardness check (they don't stop at the first failure, so warn mode measures
    them all). ({symptom_ok, spans_or_dependent, wrong_fix_ok, failed, details}, [(check, feedback)])."""
    failures: list[tuple[str, str]] = []
    details: dict = {}
    has_symptom = (world / "repro" / "symptom_check.sh").is_file()
    has_wrong = (world / "world_fix" / "wrong_fix.sh").is_file()
    missing = [p for p, ok in (("repro/symptom_check.sh", has_symptom), ("world_fix/wrong_fix.sh", has_wrong)) if not ok]
    if missing:
        details["missing"] = missing
        failures.append(("hardness_files", f"The world is missing {missing}: write them (see world.md)."))

    def run(name, script, want, msg) -> bool:
        if (feedback := expect(name, script, want, msg)) is None:
            return True
        failures.append((name, feedback))
        return False

    # The symptom an agent checks first is visible, and the real fix removes it.
    symptom_ok = has_symptom and all([
        run("symptom_visible", SYMPTOM, 1, "On the unfixed world, repro/symptom_check.sh must exit 1: it is the "
            "symptom users report, the first thing an agent reproduces."),
        run("symptom_gone_with_fix", f"{FIX} && {SYMPTOM}", 0, "After world_fix/fix.sh, repro/symptom_check.sh "
            "must exit 0."),
    ])

    # The fix must not be one local patch (lever 3): 2+ files, or a dependent the local patch breaks.
    files, out = _changed_files(check_image, FIX)
    details["fix_files"] = files
    has_dependent = (world / "repro" / "dependent_check.sh").is_file()
    if files is None:
        spans_or_dependent = False
        failures.append(("fix_not_local", f"world_fix/fix.sh failed in a fresh container:\n{out}"))
    elif len(files) >= MIN_FIX_FILES:
        spans_or_dependent = True
    elif not (has_dependent and has_wrong):
        spans_or_dependent = False
        failures.append(("fix_not_local", (
            f"world_fix/fix.sh changes only {files}: a one-file patch must not be enough. Either the correct fix "
            f"spans {MIN_FIX_FILES}+ modules, or another module depends on the current behavior: then add "
            "repro/dependent_check.sh, which exits 0 on the world and after fix.sh, and 1 after "
            "world_fix/wrong_fix.sh (the local patch breaks the dependent).")))
    else:
        spans_or_dependent = all([
            run("dependent_holds", DEPENDENT, 0, "repro/dependent_check.sh must exit 0 on the unfixed world (the "
                "dependent works today)."),
            run("dependent_holds_with_fix", f"{FIX} && {DEPENDENT}", 0, "After world_fix/fix.sh, "
                "repro/dependent_check.sh must exit 0 (the real fix keeps the dependent working)."),
            run("dependent_breaks_with_wrong_fix", f"{WRONG} && {DEPENDENT}", 1, "After world_fix/wrong_fix.sh "
                "(the local patch), repro/dependent_check.sh must exit 1: the dependent breaks."),
        ])
    details["via"] = "files" if files and len(files) >= MIN_FIX_FILES else ("dependent" if has_dependent else None)

    # The plausible wrong fix passes the agent's own check and still breaks an invariant.
    wrong_fix_ok = has_wrong and has_symptom and all([
        run("wrong_fix_passes_symptom", f"{WRONG} && {SYMPTOM}", 0, "After world_fix/wrong_fix.sh, "
            "repro/symptom_check.sh must exit 0: the plausible wrong fix makes the visible symptom go away, as an "
            "agent checking its own patch would see."),
        run("wrong_fix_fails_repro", f"{WRONG} && {REPRO}", 1, "After world_fix/wrong_fix.sh, repro/repro.sh must "
            "exit 1: the wrong fix still violates an invariant on a deeper schedule."),
    ])
    return ({"symptom_ok": bool(symptom_ok), "spans_or_dependent": spans_or_dependent,
             "wrong_fix_ok": bool(wrong_fix_ok), "failed": [n for n, _ in failures], "details": details}, failures)


def run_world_check_job(world_dir: str | Path, trace_dir: str | Path) -> dict:
    """Check one world. Returns {outcome, failed_check, result: {checks}, feedback, cost_usd}."""
    world, trace_dir = Path(world_dir), Path(trace_dir)
    record = trace_dir / "world_check.json"
    if record.exists():
        return {**json.loads(record.read_text()), "adopted": True}

    checks: dict[str, dict] = {}

    def finish(failed: str | None, feedback: str = "", hardness: dict | None = None) -> dict:
        result = {"checks": checks, **({"hardness": hardness} if hardness is not None else {})}
        out = {"outcome": "FAILED" if failed else "OK", "failed_check": failed, "result": result,
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
        def expect(name: str, script: str, want: int, msg: str, runs: int = 1) -> str | None:
            """Run `script` `runs` times; it must exit `want` each time. The feedback if not, else None."""
            got = [_run(check_image, script) for _ in range(runs)]
            passed = all(c == want for c, _ in got)
            checks[name] = {"passed": passed, "exit_codes": [c for c, _ in got], "output": "" if passed else got[-1][1]}
            if passed:
                return None
            return (f"{msg} It exited {[c for c, _ in got]} (other codes than 0/1 mean the script itself "
                    f"failed).\nOutput of the last run:\n{got[-1][1]}")

        for name, script, want, msg in (
            ("repro_fails", REPRO, 1, "On the unfixed world, repro/repro.sh must exit 1 (an invariant is violated) "
             "every run: the counterexample must be real and deterministic."),
            ("repro_passes_with_fix", f"{FIX} && {REPRO}", 0, "After world_fix/fix.sh, repro/repro.sh must exit 0 "
             "(the invariants hold) every run."),
        ):
            if (feedback := expect(name, script, want, msg, RUNS)) is not None:
                return finish(name, feedback)

        mode = hardness_mode()
        if mode == "off":
            return finish(None)
        hardness, failures = _hardness(world, check_image, expect)
        hardness["mode"] = mode
        if mode == "enforce" and failures:
            name, feedback = failures[0]
            more = f"\n\n(Also failing: {', '.join(n for n, _ in failures[1:])}.)" if failures[1:] else ""
            return finish(name, feedback + more, hardness)
        return finish(None, "", hardness)
    finally:
        _docker(["rmi", "-f", check_image, image], 120)
