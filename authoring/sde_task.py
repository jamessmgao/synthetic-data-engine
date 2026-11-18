"""sde-task: the task author's self-check, run inside the authoring container.

    sde-task check <draft.json> [--world <world_dir>]
    sde-task run <draft.json | bundle_dir> --as oracle|nop|wrong [--world <world_dir>]

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
reported as such. Prints {as, reward, expected, ok, tests, setup_errors, output_tail};
exit 0 iff ok.

Everything built or started is labeled with $SDE_DOCKER_LABEL and removed after.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

SDE = Path(__file__).resolve().parent.parent  # /opt/sde in the image, the repo root locally
sys.path[:0] = [str(SDE / "generator"), str(SDE / "validator")]

from pydantic import ValidationError  # noqa: E402

from shared.taskformat.materialize import materialize  # noqa: E402
from shared.taskformat.schema import AGENT_USER, TaskDraft  # noqa: E402

DEFAULT_WORLD = Path("/input/world")
EXPECTED = {"oracle": 1.0, "nop": 0.0, "wrong": 0.0}
SCRIPT = {"oracle": "solve.sh", "wrong": "wrong_fix.sh"}
RUN_LIMITS = ["--cpus", "1", "--memory", "2g"]
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
    try:
        p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
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

    errors = []
    if world_dir is not None and (world_dir / "world.json").is_file():
        plausible = (json.loads((world_dir / "world.json").read_text()).get("plausible_wrong_fix") or "").strip()
        if not draft.wrong_fix_script.strip():
            errors.append("wrong_fix_script is empty: it must implement the world's plausible_wrong_fix")
        elif plausible:
            quoted = [_norm(q) for q in WRONG_FIX_TAG.findall(draft.wrong_fix_script)]
            if _norm(plausible) not in quoted:
                errors.append("wrong_fix_script must implement the world's plausible_wrong_fix and say so in a "
                              f"comment line `# plausible_wrong_fix: {plausible}` (quoted from world.json)")
    if errors:
        return {"ok": False, "errors": errors, "bundle_dir": None}
    bundle = materialize(draft, out_dir / draft.name)
    return {"ok": True, "errors": [], "bundle_dir": str(bundle)}


def _tests(ctrf: dict | None) -> list[dict]:
    tests = ((ctrf or {}).get("results") or {}).get("tests") or []
    return [{"name": (t.get("name") or "").split("::")[-1], "status": t.get("status"),
             "message": (t.get("message") or t.get("trace") or "")[:800]} for t in tests]


def run_bundle(bundle: Path, role: str) -> dict:
    """Run the bundle's verifier after the role's script; see the module docstring."""
    expected = EXPECTED[role]
    image, cid = f"sde-task-{uuid.uuid4().hex[:10]}", None
    result: dict = {"as": role, "reward": None, "expected": expected, "ok": False, "tests": [],
                    "setup_errors": [], "output_tail": ""}
    log: list[str] = []
    try:
        if role == "wrong" and not (bundle / "solution" / "wrong_fix.sh").is_file():
            result["setup_errors"] = ["the bundle has no solution/wrong_fix.sh (wrong_fix_script is empty)"]
            return result
        code, out = _docker(["build", "-q", *_labels(), "-t", image, str(bundle / "environment")])
        if code != 0:
            result["setup_errors"] = [f"environment/ does not build (exit {code})"]
            log.append(out)
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
        _docker(["rmi", "-f", image], 120)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sde-task", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    check = sub.add_parser("check", help="validate a draft and materialize it")
    check.add_argument("draft", type=Path)
    check.add_argument("--world", type=Path, default=None)
    run = sub.add_parser("run", help="run the verifier after oracle / nop / wrong fix")
    run.add_argument("target", type=Path, help="draft.json or a materialized bundle dir")
    run.add_argument("--as", dest="role", choices=sorted(EXPECTED), required=True)
    run.add_argument("--world", type=Path, default=None)
    args = parser.parse_args(argv)
    world = args.world or (DEFAULT_WORLD if DEFAULT_WORLD.is_dir() else None)

    with tempfile.TemporaryDirectory(prefix="sde-task-") as tmp:
        if args.cmd == "check":
            result = check_draft(args.draft, world, Path(tmp))
        elif args.target.is_dir():
            result = run_bundle(args.target, args.role)
        else:
            checked = check_draft(args.target, world, Path(tmp))
            result = run_bundle(Path(checked["bundle_dir"]), args.role) if checked["ok"] else {
                "as": args.role, "ok": False, "setup_errors": ["the draft fails `sde-task check`"], **checked}
        print(json.dumps(result, indent=1))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
