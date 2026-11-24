"""sde-task: the task author's self-check, run inside the authoring container.

    sde-task schema
    sde-task check <draft.json> [--world <world_dir>]
    sde-task run <draft.json | bundle_dir> --as oracle|nop|wrong|oracle,nop,wrong|all [--world <world_dir>]

`schema` prints the TaskDraft JSON schema: every field draft.json must have.

`check` runs every deterministic check the pipeline runs on a TaskDraft (schema,
paths, Dockerfile, syntax, stdlib + pytest only test imports, coverage, leaks...),
plus one tie to the world: the wrong fix must be the world's plausible wrong fix,
so wrong_fix_script has to quote world.json's `plausible_wrong_fix` in a comment
line `# plausible_wrong_fix: <text>`. It then materializes the bundle.
Prints {ok, errors, bundle_dir}; exit 0 iff ok.

`run` is a small Harbor through the host Docker socket: build environment/, start a
container capped like the evaluation (1 CPU / 2 GB), copy solution/ in and run the
role's script as the `agent` user (oracle: solve.sh, wrong: wrong_fix.sh, nop:
nothing), copy tests/ in and run test.sh as root, read the reward and the per-test
results. nop and wrong must fail BECAUSE of the bug: a failure from a setup error
(ImportError, a missing file or module, no verdict at all) doesn't count, and is
reported as such. Prints {as, reward, expected, ok, tests, setup_errors, output_tail,
image_reused} for one role, {ok, runs: [...]} for several; exit 0 iff ok.

environment/ is built once: several roles run in fresh containers from one image, and
inside an authoring trial ($SDE_DOCKER_LABEL set) the image is tagged by the content
of environment/ and kept, so a later `sde-task run` on the same environment skips the
build (the harness prunes labeled images after the trial).

Everything built or started is labeled with $SDE_DOCKER_LABEL and removed after.
Each check / run result is also saved to $SDE_ARTIFACTS_DIR/sde_task/<check|run-ROLE>.json
(default /logs/artifacts, if it exists): the pipeline reads those as the evidence
that the author self-checked. Each carries a `stamp`, the draft's content hash
(materialize.content_sha256): a saved result counts only for the exact final draft, and
validate may adopt the runs instead of repeating them (SDE_ADOPT_AUTHOR_CHECKS). A run on a
bundle dir has no draft, so no stamp: the pipeline runs it again.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SDE = Path(__file__).resolve().parent.parent  # /opt/sde in the image, the repo root locally
if not __package__:  # run by path (bin/sde-task): the root goes on the import path, in place of this directory
    _here = Path(__file__).resolve().parent
    sys.path[:] = [str(_here.parent)] + [p for p in sys.path if p != str(_here)]

from pydantic import ValidationError  # noqa: E402

from shared.taskformat.materialize import content_sha256, materialize  # noqa: E402
from shared.taskformat.schema import AGENT_USER, TaskDraft  # noqa: E402

DEFAULT_WORLD = Path("/input/world")
EXPECTED = {"oracle": 1.0, "nop": 0.0, "wrong": 0.0}
SCRIPT = {"oracle": "solve.sh", "wrong": "wrong_fix.sh"}
RUN_LIMITS = ["--cpus", "1", "--memory", "2g"]
# Roles' containers run in parallel on the one image. Each is capped at 1 CPU / 2 GB, so on
# a 2-vCPU / 8 GB instance (shared with other pods) more than 2 at once risks memory.
PARALLEL = max(1, int(os.environ.get("SDE_TASK_PARALLEL", "2")))
STEP_TIMEOUT_SEC = 1800
OUTPUT_CHARS = 3000
# A failing test whose message says one of these failed for a setup reason, not the bug.
SETUP_ERROR = re.compile(r"\b(ImportError|ModuleNotFoundError|FileNotFoundError|No such file or directory|"
                         r"command not found|Permission denied)\b")
WRONG_FIX_TAG = re.compile(r"^\s*#\s*plausible_wrong_fix:\s*(.+?)\s*$", re.M)


def _labels() -> list[str]:
    label = os.environ.get("SDE_DOCKER_LABEL")
    return ["--label", label] if label else []


def _docker(args: list[str], timeout: float = STEP_TIMEOUT_SEC) -> tuple[int, str]:
    # The real CLI (world_check.docker_bin): `docker` in the image is the agent's
    # restricted wrapper; sde-task labels everything it starts.
    from shared.taskformat.world_check import docker_bin

    try:
        p = subprocess.run([docker_bin(), *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr)
    except subprocess.TimeoutExpired:
        return -1, f"docker {args[0]} timed out after {timeout}s"


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _format(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "draft"
        out += [f"{loc}: {part}" for part in err["msg"].removeprefix("Value error, ").split("; ")]
    return out


INVARIANT_TAG = re.compile(r"^\s*#\s*invariant:\s*(\d+)\b", re.M)
# The module-level random functions draw from the global, time-seeded generator.
GLOBAL_RANDOM = re.compile(r"\brandom\.(random|randint|randrange|choice|choices|shuffle|sample|uniform|gauss|"
                           r"getrandbits)\s*\(")
TIME_SEED = re.compile(r"\b(?:random\.seed|Random)\s*\(\s*(?:int\s*\()?\s*(?:time\.|datetime\.|os\.urandom|uuid)")


def unseeded_randomness(tests: str) -> list[str]:
    """Randomized tests must replay identically: a seeded random.Random(seed) per case.
    Unseeded global random.* calls, random.Random() and time-based seeds are refused (a
    random.seed(<constant>) before global calls is accepted)."""
    errors = []
    seeded_globally = re.search(r"\brandom\.seed\s*\(\s*[\w\"']", tests) and not TIME_SEED.search(tests)
    if GLOBAL_RANDOM.search(tests) and not seeded_globally:
        errors.append("the tests call the global random.* functions without a fixed seed: use "
                      "`rng = random.Random(seed)` with fixed seeds (e.g. `for seed in range(20)`), so every run "
                      "replays the same schedules")
    if re.search(r"\bRandom\s*\(\s*\)", tests):
        errors.append("random.Random() without a seed is time-seeded: pass a fixed seed")
    if TIME_SEED.search(tests):
        errors.append("a seed taken from the clock or os.urandom makes the tests differ per run: use fixed seeds")
    return errors


def check_draft(draft_path: Path, world_dir: Path | None, out_dir: Path) -> dict:
    """Validate a draft and materialize it. {ok, errors, bundle_dir}."""
    try:
        raw = json.loads(draft_path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        return {"ok": False, "errors": [f"cannot read {draft_path}: {e}"], "bundle_dir": None}
    try:
        draft = TaskDraft.model_validate(raw)
    except ValidationError as e:
        return {"ok": False, "errors": _format(e), "bundle_dir": None}

    stamp = content_sha256(draft)
    errors = []
    tests = "\n".join([draft.test_outputs, *(f.content for f in draft.test_support_files
                                             if f.path.endswith(".py"))])
    errors += unseeded_randomness(tests)
    if world_dir is not None and (world_dir / "world.json").is_file():
        invariants = json.loads((world_dir / "world.json").read_text()).get("invariants") or []
        marked = {int(n) for n in INVARIANT_TAG.findall(tests)}
        for n, text in enumerate(invariants, 1):
            if n not in marked:
                errors.append(f"no test is marked `# invariant: {n}` (\"{text}\"): give each world invariant a "
                              "dedicated test, checking it after every step of seeded randomized schedules")
        if extra := sorted(m for m in marked if not 1 <= m <= len(invariants)):
            errors.append(f"`# invariant:` markers {extra} name no invariant (the world has {len(invariants)})")
        plausible = (json.loads((world_dir / "world.json").read_text()).get("plausible_wrong_fix") or "").strip()
        if not draft.wrong_fix_script.strip():
            errors.append("wrong_fix_script is empty: it must implement the world's plausible_wrong_fix")
        elif plausible:
            quoted = [_norm(q) for q in WRONG_FIX_TAG.findall(draft.wrong_fix_script)]
            if _norm(plausible) not in quoted:
                errors.append("wrong_fix_script must implement the world's plausible_wrong_fix and say so in a "
                              f"comment line `# plausible_wrong_fix: {plausible}` (quoted from world.json)")
    if errors:
        return {"ok": False, "errors": errors, "bundle_dir": None, "stamp": stamp}
    bundle = materialize(draft, out_dir / draft.name)
    return {"ok": True, "errors": [], "bundle_dir": str(bundle), "stamp": stamp}


def _tests(ctrf: dict | None) -> list[dict]:
    tests = ((ctrf or {}).get("results") or {}).get("tests") or []
    return [{"name": (t.get("name") or "").split("::")[-1], "status": t.get("status"),
             "message": (t.get("message") or t.get("trace") or "")[:800]} for t in tests]


def env_digest(env_dir: Path) -> str:
    """A content hash of environment/ (paths, bytes and the executable bit)."""
    h = hashlib.sha256()
    for p in sorted(env_dir.rglob("*")):
        if p.is_file():
            h.update(str(p.relative_to(env_dir)).encode() + b"\0" + bytes([os.access(p, os.X_OK)]))
            h.update(p.read_bytes() + b"\0")
    return h.hexdigest()[:16]


def _keep_images() -> bool:
    """Inside an authoring trial ($SDE_DOCKER_LABEL set) an image is kept for the next run of
    the same environment/ (the harness prunes labeled images after the trial)."""
    return bool(os.environ.get("SDE_DOCKER_LABEL"))


def build_image(bundle: Path) -> tuple[str | None, str, bool]:
    """(image, build output, reused): one image per environment/ content, built once and
    reused by every role and every later `sde-task run` in the trial. None if the build fails."""
    image = f"sde-task-env:{env_digest(bundle / 'environment')}" if _keep_images() \
        else f"sde-task-{uuid.uuid4().hex[:10]}"
    if _keep_images() and _docker(["image", "inspect", image], 60)[0] == 0:
        return image, "", True
    code, out = _docker(["build", "-q", *_labels(), "-t", image, str(bundle / "environment")])
    return (image if code == 0 else None), out, False


def run_roles(bundle: Path, roles: list[str], repeat_oracle: bool = False) -> list[dict]:
    """Every role on one image: built once, a fresh container per role. With
    `repeat_oracle`, the oracle runs twice and must give the same per-test results
    (the flake check: seeded tests replay identically)."""
    image, out, reused = build_image(bundle)
    try:
        if image is None:
            failed = {"reward": None, "ok": False, "tests": [], "output_tail": out[-OUTPUT_CHARS:],
                      "setup_errors": ["environment/ does not build"], "image_reused": False}
            return [{"as": r, "expected": EXPECTED[r], **failed} for r in roles]
        # Each role in its own fresh container on the same image, up to PARALLEL at once; the
        # oracle's repeat (the flake check) is one more independent run.
        jobs = [(r, r) for r in roles] + ([("oracle (repeat)", "oracle")] if repeat_oracle and "oracle" in roles else [])
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(PARALLEL, len(jobs))) as pool:
            done = dict(zip([name for name, _ in jobs],
                            pool.map(lambda job: _timed_run(bundle, job[0], job[1], image, t0), jobs)))
        print(f"[sde-task] all roles done in {time.monotonic() - t0:.0f}s", file=sys.stderr, flush=True)
        runs = [{**done[r], "image_reused": reused or i > 0} for i, r in enumerate(roles)]
        if repeat_oracle and "oracle" in roles:
            first = next(r for r in runs if r["as"] == "oracle")
            again = done["oracle (repeat)"]
            outcome = lambda r: {t["name"]: t["status"] for t in r["tests"]}  # noqa: E731
            changed = sorted(n for n in outcome(first).keys() | outcome(again).keys()
                             if outcome(first).get(n) != outcome(again).get(n))
            first["repeat"] = {"reward": again["reward"], "same": not changed and again["reward"] == first["reward"]}
            if not first["repeat"]["same"]:
                first["ok"] = False
                first["setup_errors"].append("flaky: a second oracle run on the same image gave different results"
                                             + (f" for {changed}" if changed else "") + ": seed every randomized "
                                             "test and remove timing dependence")
        return runs
    finally:
        if image is not None and not _keep_images():
            _docker(["rmi", "-f", image], 120)


def _timed_run(bundle: Path, name: str, role: str, image: str, t0: float) -> dict:
    """run_bundle with its wall time (`duration_sec`) and a progress line on stderr as it
    finishes: an agent waiting on `sde-task run --as all` once saw nothing for 11 min."""
    start = time.monotonic()
    result = run_bundle(bundle, role, image=image)
    result["duration_sec"] = round(time.monotonic() - start, 1)
    print(f"[sde-task] {name}: reward={result['reward']} ok={result['ok']} {result['duration_sec']}s "
          f"(elapsed {time.monotonic() - t0:.0f}s)", file=sys.stderr, flush=True)
    return result


def run_bundle(bundle: Path, role: str, image: str | None = None) -> dict:
    """Run the bundle's verifier after the role's script; see the module docstring. With
    `image`, on that already built image (run_roles); else build one for this run."""
    if image is None:
        return run_roles(bundle, [role])[0]
    expected = EXPECTED[role]
    cid = None
    result: dict = {"as": role, "reward": None, "expected": expected, "ok": False, "tests": [],
                    "setup_errors": [], "output_tail": ""}
    log: list[str] = []
    try:
        if role == "wrong" and not (bundle / "solution" / "wrong_fix.sh").is_file():
            result["setup_errors"] = ["the bundle has no solution/wrong_fix.sh (wrong_fix_script is empty)"]
            return result
        code, out = _docker(["run", "-d", *RUN_LIMITS, *_labels(), "--entrypoint", "sleep", image, "infinity"])
        if code != 0:
            result["setup_errors"] = [f"the container does not start: {out.strip()[-500:]}"]
            return result
        cid = out.strip().splitlines()[-1]
        _docker(["exec", "-u", "root", cid, "mkdir", "-p", "/logs/verifier", "/logs/agent"])
        if role in SCRIPT:
            _docker(["cp", str(bundle / "solution"), f"{cid}:/solution"])
            _docker(["exec", "-u", "root", cid, "chown", "-R", AGENT_USER, "/solution"])
            code, out = _docker(["exec", "-u", AGENT_USER, cid, "bash", f"/solution/{SCRIPT[role]}"])
            log.append(f"$ {SCRIPT[role]} (exit {code})\n{out}")
        _docker(["cp", str(bundle / "tests"), f"{cid}:/tests"])
        code, out = _docker(["exec", "-u", "root", cid, "bash", "/tests/test.sh"])
        log.append(f"$ test.sh (exit {code})\n{out}")
        _, reward = _docker(["exec", cid, "cat", "/logs/verifier/reward.txt"])
        try:
            result["reward"] = float(reward.strip())
        except ValueError:
            result["setup_errors"].append("the verifier wrote no reward: pytest crashed or collected nothing")
        _, ctrf = _docker(["exec", cid, "cat", "/logs/verifier/ctrf.json"])
        try:
            result["tests"] = _tests(json.loads(ctrf))
        except json.JSONDecodeError:
            pass
        failing = [t for t in result["tests"] if t["status"] not in ("passed", "skipped")]
        setup = [t for t in failing if SETUP_ERROR.search(t["message"] or "")]
        if expected == 0.0 and failing and len(setup) == len(failing):
            result["setup_errors"].append(
                "every failing test failed for a setup reason (import, missing file or command), not because "
                "of the bug: " + ", ".join(t["name"] for t in setup))
        result["ok"] = result["reward"] == expected and not result["setup_errors"]
        return result
    finally:
        result["output_tail"] = "\n".join(log)[-OUTPUT_CHARS:]
        if cid:
            _docker(["rm", "-f", cid], 120)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sde-task", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("schema", help="print the TaskDraft JSON schema draft.json must match")
    check = sub.add_parser("check", help="validate a draft and materialize it")
    check.add_argument("draft", type=Path)
    check.add_argument("--world", type=Path, default=None)
    run = sub.add_parser("run", help="run the verifier after oracle / nop / wrong fix")
    run.add_argument("target", type=Path, help="draft.json or a materialized bundle dir")
    run.add_argument("--as", dest="role", type=_roles, required=True,
                     help="oracle, nop, wrong, a comma list (oracle,nop,wrong) or all: one image for every role")
    run.add_argument("--world", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.cmd == "schema":
        print(json.dumps(TaskDraft.model_json_schema(), indent=1))
        return 0
    world = args.world or (DEFAULT_WORLD if DEFAULT_WORLD.is_dir() else None)

    with tempfile.TemporaryDirectory(prefix="sde-task-") as tmp:
        if args.cmd == "check":
            result = check_draft(args.draft, world, Path(tmp))
            save("check.json", json.dumps(result, indent=1))
            print(json.dumps(result, indent=1))
            return 0 if result["ok"] else 1
        bundle = args.target
        stamp = None  # a run on a bundle dir can't be tied to a draft: the pipeline re-runs it
        if not bundle.is_dir():
            checked = check_draft(args.target, world, Path(tmp))
            stamp = checked.get("stamp")
            if not checked["ok"]:
                runs = [{"as": r, "ok": False, "setup_errors": ["the draft fails `sde-task check`"], **checked}
                        for r in args.role]
            bundle = Path(checked["bundle_dir"]) if checked["ok"] else None
        if bundle is not None:
            # The full self-check (all three roles) also runs the oracle twice: the flake check.
            runs = run_roles(bundle, args.role, repeat_oracle=set(args.role) == set(EXPECTED))
        for r in runs:
            r["stamp"] = stamp
            save(f"run-{r['as']}.json", json.dumps(r, indent=1))
        result = runs[0] if len(runs) == 1 else {"ok": all(r["ok"] for r in runs), "runs": runs}
        print(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


def _roles(value: str) -> list[str]:
    roles = list(EXPECTED) if value == "all" else [r.strip() for r in value.split(",") if r.strip()]
    bad = [r for r in roles if r not in EXPECTED]
    if bad or not roles:
        raise argparse.ArgumentTypeError(f"roles are {', '.join(EXPECTED)} or all, not {value!r}")
    return list(dict.fromkeys(roles))


def save(name: str, text: str) -> None:
    """Keep the latest result where the pipeline looks for self-check evidence."""
    artifacts = Path(os.environ.get("SDE_ARTIFACTS_DIR", "/logs/artifacts"))
    if artifacts.is_dir():
        (artifacts / "sde_task").mkdir(exist_ok=True)
        (artifacts / "sde_task" / name).write_text(text + "\n")


if __name__ == "__main__":
    sys.exit(main())
