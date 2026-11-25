"""
Functional gates for a task bundle: the `validate` job of PRD §6.3.

    structural  validate.py (layout + task.toml schema)
    lint        harbor lint --json; config.LINT_BLOCKING_RULES block, the rest warn
    oracle1/2   reference solution: reward 1, twice (catches flaky tests)
    nop         untouched container: reward exactly 0, with a real verdict
    wrong_fix   the author's plausible wrong fix, run as the oracle: reward exactly 0
    leak        glm-5.1: a semantic review against the hidden spec and the world, plus a
                blind fix prediction from the instruction alone: the instruction must not
                reveal the fix or the failing sequence (docs/VALIDATION.md)
                (shared/taskformat/leak.py: the create job runs it first, validate adopts it)

Gates run cheapest first and stop at the first failure. Each failure carries
``feedback``: the exact evidence (failing tests and assertion messages, verifier
output, the oracle's own output, Harbor exception) that a repair round sends
back to the authoring model.

Each Harbor step runs as ``harbor run --job-name <step> -o <trace_dir>``, so
the steps of one validate job live under ``traces/<job_id>/{oracle1,oracle2,nop}``.
A step whose finished result is already on disk is adopted instead of rerun;
a half-finished one is renamed ``<step>.abandoned-<n>`` and kept for the audit.

Usage: python gates.py <bundle_dir> <trace_dir>
"""

import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

if not __package__:  # run by path (python validator/gates.py): the repo root goes on the import path, in place of this directory
    _here = Path(__file__).resolve().parent
    sys.path[:] = [str(_here.parent)] + [p for p in sys.path if p != str(_here)]

from shared.contracts import config  # noqa: E402
from validator.validate import validate_task  # noqa: E402
from shared.taskformat.harbor_networks import remove_trial_networks  # noqa: E402
from shared.taskformat.leak import Gate, gate_leak  # noqa: E402  (the leak gate: also run by the create job)
from shared.taskformat.world_check import run_world_check_job  # noqa: E402,F401  (the world_check job, re-exported)

TAIL_CHARS = 3000


@dataclass
class TrialOutcome:
    """What one Harbor trial showed, read from its result.json and logs."""
    trial_dir: str | None
    reward: float | None
    exception_type: str | None
    exception_message: str | None
    tests: list[dict]  # [{name, status, message}] from ctrf.json
    adopted: bool


def _tail(path: Path, chars: int = TAIL_CHARS) -> str:
    if not path.exists():
        return ""
    text = path.read_text(errors="replace")
    return text[-chars:]


def _read_trial(job_dir: Path) -> tuple[Path | None, dict | None]:
    trials = sorted(p.parent for p in job_dir.glob("*/result.json"))
    if not trials:
        return None, None
    return trials[0], json.loads((trials[0] / "result.json").read_text())


def _ctrf_tests(trial_dir: Path) -> list[dict]:
    path = trial_dir / "verifier" / "ctrf.json"
    if not path.exists():
        return []
    tests = json.loads(path.read_text()).get("results", {}).get("tests", [])
    return [{"name": t.get("name", "").split("::")[-1], "status": t.get("status"),
             "message": (t.get("message") or t.get("trace") or "")[:800]} for t in tests]


def run_harbor_step(bundle_dir: Path, agent: str, trace_dir: Path, step: str) -> TrialOutcome:
    """Run (or adopt) one oracle/nop trial under trace_dir/step."""
    job_dir = trace_dir / step
    adopted = False
    if job_dir.exists():
        if (job_dir / "result.json").exists() and _read_trial(job_dir)[1] is not None:
            adopted = True
        else:
            n = 1
            while job_dir.with_name(f"{step}.abandoned-{n}").exists():
                n += 1
            job_dir.rename(job_dir.with_name(f"{step}.abandoned-{n}"))

    if not adopted:
        trace_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [config.HARBOR_BIN, "run", "-p", str(bundle_dir), "-a", agent, "-o", str(trace_dir),
                 "--job-name", step, "-y", "-q"],
                capture_output=True, text=True, timeout=config.GATE_TIMEOUT_SEC,
            )
        finally:
            # A killed, crashed or timed-out Harbor never runs compose down: its trial
            # network leaks, and leaked networks exhausted Docker's address pools.
            remove_trial_networks(job_dir)

    trial_dir, result = _read_trial(job_dir)
    if result is None:
        return TrialOutcome(None, None, "NoTrialResult", f"harbor wrote no trial result under {job_dir}",
                            [], adopted)
    exc = result.get("exception_info") or {}
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    return TrialOutcome(
        trial_dir=str(trial_dir),
        reward=rewards.get("reward"),
        exception_type=exc.get("exception_type"),
        # The tail: for a failed image build, the compiler error is at the end
        # of a long BuildKit log.
        exception_message=(exc.get("exception_message") or "")[-TAIL_CHARS:] or None,
        tests=_ctrf_tests(trial_dir),
        adopted=adopted,
    )


def _trial_evidence(outcome: TrialOutcome, include_agent_output: bool) -> str:
    lines = []
    if outcome.exception_type:
        lines.append(f"Harbor exception: {outcome.exception_type}: {outcome.exception_message}")
    failing = [t for t in outcome.tests if t["status"] != "passed"]
    for t in failing:
        lines.append(f"FAILED {t['name']}: {t['message']}")
    if outcome.trial_dir:
        trial = Path(outcome.trial_dir)
        if include_agent_output:
            exit_code = _tail(trial / "agent" / "exit-code.txt", 50).strip()
            if exit_code:
                lines.append(f"solve.sh exited with status {exit_code}")
            oracle_out = _tail(trial / "agent" / "oracle.txt", 1500)
            if oracle_out:
                lines.append(f"solve.sh output (tail):\n{oracle_out}")
        verifier_out = _tail(trial / "verifier" / "test-stdout.txt")
        if verifier_out and (failing or outcome.reward is None):
            lines.append(f"verifier output (tail):\n{verifier_out}")
    return "\n".join(lines)


def gate_structural(bundle_dir: Path) -> Gate:
    r = validate_task(str(bundle_dir))
    return Gate("structural", r["passed"], {"errors": r["errors"], "warnings": r["warnings"]},
                "" if r["passed"] else "Structural validator errors:\n" + "\n".join(f"- {e}" for e in r["errors"]))


def gate_lint(bundle_dir: Path) -> Gate:
    # --select ALL: several blocking rules (root-agent, unpinned-image) are not
    # in harbor's default selection.
    proc = subprocess.run([config.HARBOR_BIN, "lint", str(bundle_dir), "--json", "--select", "ALL"],
                          capture_output=True, text=True, timeout=300)
    try:
        findings = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return Gate("lint", False, {"stderr": proc.stderr[-TAIL_CHARS:]},
                    f"harbor lint failed to run: {proc.stderr[-TAIL_CHARS:]}")
    blocking = [f for f in findings if f.get("rule") in config.LINT_BLOCKING_RULES]
    detail = {"blocking": blocking, "warnings": [f for f in findings if f not in blocking]}
    feedback = "\n".join(f"- [{f.get('rule')}] {f.get('path', '')}: {f.get('message', '')}" for f in blocking)
    return Gate("lint", not blocking, detail, f"harbor lint findings that must be fixed:\n{feedback}" if blocking else "")


def gate_oracle(bundle_dir: Path, trace_dir: Path, step: str) -> Gate:
    o = run_harbor_step(bundle_dir, "oracle", trace_dir, step)
    passed = o.exception_type is None and o.reward == 1.0
    feedback = "" if passed else (
        f"The reference solution did not pass ({step}: reward {o.reward}).\n" + _trial_evidence(o, True))
    return Gate(step, passed, asdict(o), feedback)


def gate_nop(bundle_dir: Path, trace_dir: Path) -> Gate:
    o = run_harbor_step(bundle_dir, "nop", trace_dir, "nop")
    passed = o.exception_type is None and o.reward == 0.0
    passing = [t["name"] for t in o.tests if t["status"] == "passed"]
    detail = {**asdict(o), "tests_passing_on_nop": passing}
    if passed:
        return Gate("nop", True, detail)
    if o.reward == 1.0:
        feedback = ("The untouched starting environment passes every test, so the tests do not detect the "
                    f"defect. Tests that passed with no changes: {passing}")
    else:
        feedback = "The untouched environment did not produce a clean verdict.\n" + _trial_evidence(o, False)
    return Gate("nop", False, detail, feedback)


def _wrong_fix_bundle(bundle_dir: Path, trace_dir: Path) -> Path:
    """A copy of the bundle whose solve.sh is the wrong fix, under the job's trace dir."""
    variant = trace_dir / "wrong_fix_bundle"
    if not variant.exists():
        tmp = trace_dir / ".wrong_fix_bundle.tmp"
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.copytree(bundle_dir, tmp)
        wrong = tmp / "solution" / "wrong_fix.sh"
        (tmp / "solution" / "solve.sh").write_text(wrong.read_text())
        (tmp / "solution" / "solve.sh").chmod(0o755)
        wrong.unlink()
        tmp.rename(variant)
    return variant


def gate_wrong_fix(bundle_dir: Path, trace_dir: Path) -> Gate:
    """The plausible wrong fix must FAIL the tests (PRD decision D3).

    oracle + nop prove the tests pass the right fix and fail no fix; this proves
    they also reject the fix a capable agent would most likely write first.
    """
    wrong = bundle_dir / "solution" / "wrong_fix.sh"
    if not wrong.exists():
        return Gate("wrong_fix", True, {"skipped": "the bundle has no solution/wrong_fix.sh"})
    o = run_harbor_step(_wrong_fix_bundle(bundle_dir, trace_dir), "oracle", trace_dir, "wrong_fix")
    detail = asdict(o)
    if o.exception_type is None and o.reward == 0.0:
        return Gate("wrong_fix", True, detail)
    script = wrong.read_text()[-1500:]
    if o.reward == 1.0:
        feedback = ("The tests ACCEPT the plausible wrong fix (solution/wrong_fix.sh), so a capable agent's "
                    "first, incomplete fix would pass. Make the tests detect what this fix breaks (a stated "
                    "requirement it violates), stating that requirement in the instruction if it is not "
                    "already there, while the reference solution keeps passing.\n\nThe wrong fix:\n" + script)
    else:
        feedback = ("The plausible wrong fix (solution/wrong_fix.sh) did not produce a clean verdict, so it "
                    "cannot show the tests reject it. It must run to completion like solve.sh and leave the "
                    "system in a state the tests can judge.\n" + _trial_evidence(o, True))
    return Gate("wrong_fix", False, detail, feedback)


def adopted_runtime_gates(author_checks: dict | None, bundle_sha256: str | None) -> dict[str, Gate] | None:
    """oracle1, oracle2, nop and wrong_fix from the create job's own sde-task runs, when
    they may stand in for Harbor's (SDE_ADOPT_AUTHOR_CHECKS=1): the create job checked them
    against this exact bundle (its stamp is the final draft's, and its bundle sha is this
    one), and every role came out as the gate requires (the oracle at reward 1 twice with
    the same per-test results, nop and the wrong fix at 0). Else None: run them as today."""
    if not config.adopt_author_checks() or not author_checks or not bundle_sha256:
        return None
    roles = author_checks.get("roles") or {}
    stamp = author_checks.get("stamp")
    if author_checks.get("bundle_sha256") != bundle_sha256 or not stamp:
        return None
    if any((roles.get(r) or {}).get("stamp") != stamp for r in ("oracle", "nop", "wrong")):
        return None
    oracle, nop, wrong = roles.get("oracle") or {}, roles.get("nop") or {}, roles.get("wrong") or {}
    repeat = oracle.get("repeat") or {}
    if not (oracle.get("ok") and oracle.get("reward") == 1.0 and repeat.get("same") and repeat.get("reward") == 1.0
            and nop.get("ok") and nop.get("reward") == 0.0 and wrong.get("ok") and wrong.get("reward") == 0.0):
        return None
    src = {"adopted_from": author_checks.get("job_id"), "stamp": stamp}
    return {
        "oracle1": Gate("oracle1", True, {"reward": 1.0, **src}),
        "oracle2": Gate("oracle2", True, {"reward": 1.0, **src}),
        "nop": Gate("nop", True, {"reward": 0.0, **src}),
        "wrong_fix": Gate("wrong_fix", True, {"reward": 0.0, **src}),
    }


def run_validate_job(bundle_dir: str | Path, trace_dir: str | Path, author_checks: dict | None = None,
                     bundle_sha256: str | None = None) -> dict:
    """Run the gates on one bundle.

    Returns {outcome, failed_gate, result: {gates}, feedback, cost_usd}. Only the
    leak review costs money; its spend is cost_usd (0 when adopted on a retry). With
    `author_checks` (the create job's stamped sde-task runs) and SDE_ADOPT_AUTHOR_CHECKS=1,
    the Docker gates are adopted from them when they match (adopted_runtime_gates);
    structural, lint and leak always run.
    """
    bundle_dir, trace_dir = Path(bundle_dir), Path(trace_dir)
    adopted = adopted_runtime_gates(author_checks, bundle_sha256) or {}
    steps = [
        lambda: gate_structural(bundle_dir),
        lambda: gate_lint(bundle_dir),
        lambda: adopted.get("oracle1") or gate_oracle(bundle_dir, trace_dir, "oracle1"),
        lambda: adopted.get("oracle2") or gate_oracle(bundle_dir, trace_dir, "oracle2"),
        lambda: adopted.get("nop") or gate_nop(bundle_dir, trace_dir),
        lambda: adopted.get("wrong_fix") or gate_wrong_fix(bundle_dir, trace_dir),
        lambda: gate_leak(bundle_dir, trace_dir),
    ]
    gates: dict[str, dict] = {}
    for step in steps:
        gate = step()
        gates[gate.name] = {"passed": gate.passed, **gate.detail}
        cost = round(sum(g.get("cost_usd") or 0.0 for g in gates.values()), 6)
        if not gate.passed:
            return {"outcome": "FAILED", "failed_gate": gate.name, "result": {"gates": gates},
                    "feedback": gate.feedback, "cost_usd": cost}
    return {"outcome": "OK", "failed_gate": None, "result": {"gates": gates}, "feedback": "", "cost_usd": cost}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python validator/gates.py <bundle_dir> <trace_dir>")
    res = run_validate_job(sys.argv[1], sys.argv[2])
    for name, g in res["result"]["gates"].items():
        print(f"{'PASS' if g['passed'] else 'FAIL'}  {name}")
    if res["feedback"]:
        print("\n" + res["feedback"])
    sys.exit(0 if res["outcome"] == "OK" else 1)
