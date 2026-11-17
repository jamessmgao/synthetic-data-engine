"""
Functional gates for a task bundle: the `validate` job of PRD §6.3.

    structural  validate.py (layout + task.toml schema)
    lint        harbor lint --json; config.LINT_BLOCKING_RULES block, the rest warn
    oracle1/2   reference solution: reward 1, twice (catches flaky tests)
    nop         untouched container: reward exactly 0, with a real verdict

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
import os
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generator"))
from shared.contracts import config  # noqa: E402
from validator.validate import validate_task  # noqa: E402

TAIL_CHARS = 3000


@dataclass
class Gate:
    name: str
    passed: bool
    detail: dict = field(default_factory=dict)
    feedback: str = ""  # evidence for a repair round; empty when passed


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
        subprocess.run(
            [config.HARBOR_BIN, "run", "-p", str(bundle_dir), "-a", agent, "-o", str(trace_dir),
             "--job-name", step, "-y", "-q"],
            capture_output=True, text=True, timeout=config.GATE_TIMEOUT_SEC,
        )

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
        exception_message=(exc.get("exception_message") or "")[:TAIL_CHARS] or None,
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


def run_validate_job(bundle_dir: str | Path, trace_dir: str | Path) -> dict:
    """Run the gates on one bundle. Returns {outcome, result: {gates}, feedback}."""
    bundle_dir, trace_dir = Path(bundle_dir), Path(trace_dir)
    steps = [
        lambda: gate_structural(bundle_dir),
        lambda: gate_lint(bundle_dir),
        lambda: gate_oracle(bundle_dir, trace_dir, "oracle1"),
        lambda: gate_oracle(bundle_dir, trace_dir, "oracle2"),
        lambda: gate_nop(bundle_dir, trace_dir),
    ]
    gates: dict[str, dict] = {}
    for step in steps:
        gate = step()
        gates[gate.name] = {"passed": gate.passed, **gate.detail}
        if not gate.passed:
            return {"outcome": "FAILED", "failed_gate": gate.name, "result": {"gates": gates},
                    "feedback": gate.feedback}
    return {"outcome": "OK", "failed_gate": None, "result": {"gates": gates}, "feedback": ""}


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python gates.py <bundle_dir> <trace_dir>")
    res = run_validate_job(sys.argv[1], sys.argv[2])
    for name, g in res["result"]["gates"].items():
        print(f"{'PASS' if g['passed'] else 'FAIL'}  {name}")
    if res["feedback"]:
        print("\n" + res["feedback"])
    sys.exit(0 if res["outcome"] == "OK" else 1)
