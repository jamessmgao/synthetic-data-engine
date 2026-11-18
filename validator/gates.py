"""
Functional gates for a task bundle: the `validate` job of PRD §6.3.

    structural  validate.py (layout + task.toml schema)
    lint        harbor lint --json; config.LINT_BLOCKING_RULES block, the rest warn
    oracle1/2   reference solution: reward 1, twice (catches flaky tests)
    nop         untouched container: reward exactly 0, with a real verdict
    wrong_fix   the author's plausible wrong fix, run as the oracle: reward exactly 0
    leak        a glm-5.1 review: the instruction must not reveal the hidden cause or fix

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
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "generator"))
from shared.contracts import config  # noqa: E402
from validator.validate import validate_task  # noqa: E402
from shared.taskformat.world_check import run_world_check_job  # noqa: E402,F401  (the world_check job, re-exported)

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


# --- leak review ---------------------------------------------------------------
# One glm-5.1 call after the functional gates: does the instruction the agent
# reads give away the hidden root cause or the fix? In the pilots, prompt rules
# alone did not stop this (pipeline-hard, h6), and it made tasks too easy.

LEAK_SYSTEM = """You review a benchmark task before an AI agent attempts it. You get the
author's hidden notes (root cause, solution subgoals, correctness constraints)
and the instruction the agent will read. Decide whether the instruction gives
away the root cause or the fix.

A LEAK is a sentence of the instruction that tells the agent where the defect
is or what change to make. For example, it:
- names the faulty file, function, class, variable, field or mechanism;
- describes how the internal code currently misbehaves, rather than what the
  user observes;
- states a requirement in words that amount to the patch (for example "the
  check must compare against totals computed independently from raw events"
  when the bug is that it compares against the wrong totals);
- singles out exactly the cases the planted defects break (for example
  "including transactions with three or more entries" when the bug drops the
  third entry).

NOT a leak:
- user-visible symptoms;
- required observable behavior of commands, outputs and files, even when that
  behavior is what is currently broken;
- input and output formats, and names of files, commands or fields that are
  part of the interface the tests check;
- general rules that cover the broken case among many others.

Flag only real leaks. Quote each leaking sentence exactly as it appears in the
instruction, and say what it reveals. Return verdict "leak" if there is at
least one leak; otherwise "clean" with an empty list."""

LEAK_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "leak_review", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["leaks", "verdict"],
        "properties": {
            "leaks": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["quote", "reveals"],
                "properties": {"quote": {"type": "string"}, "reveals": {"type": "string"}}}},
            "verdict": {"type": "string", "enum": ["clean", "leak"]},
        }}},
}

HIDDEN_SECTIONS = ("Root cause (hidden from the agent)", "Subgoals", "Correctness constraints")


def _hidden_notes(readme: str) -> str:
    """The author's hidden spec sections from the bundle README (materialize.py)."""
    parts = []
    for title in HIDDEN_SECTIONS:
        start = readme.find(f"## {title}\n")
        if start == -1:
            continue
        end = readme.find("\n## ", start + 4)
        parts.append(readme[start:end if end != -1 else None].strip())
    return "\n\n".join(parts)


def _normalized(text: str) -> str:
    return " ".join(text.replace("`", "").replace("*", "").split()).lower()


def leak_review_dir(bundle_dir: Path, trace_dir: Path) -> Path:
    """Where a bundle's leak review is recorded. A content-addressed bundle
    (<work>/bundles/<sha>) keeps it under <work>/leak_reviews/<sha>, so every job that
    reviews the same bundle adopts it: the create job reviews it first (plan N6, no
    Docker slot), validate reuses that review. Any other directory: <trace_dir>/leak."""
    bundle_dir = Path(bundle_dir)
    if bundle_dir.parent.name == "bundles":
        return bundle_dir.parent.parent / "leak_reviews" / bundle_dir.name
    return Path(trace_dir) / "leak"


def gate_leak(bundle_dir: Path, trace_dir: Path) -> Gate:
    """Reject a bundle whose instruction reveals the hidden cause or fix.

    The review is stored at leak_review_dir()/review.json and adopted on retry and
    by any later job on the same bundle. A leak only counts if its quote really
    appears in the instruction, so an over-eager or hallucinating reviewer cannot
    fail a task on its own words.
    """
    if not config.LEAK_REVIEW:
        return Gate("leak", True, {"skipped": "disabled (SDE_LEAK_REVIEW=0)", "cost_usd": 0.0})
    readme = bundle_dir / "README.md"
    notes = _hidden_notes(readme.read_text()) if readme.exists() else ""
    if not notes:
        return Gate("leak", True, {"skipped": "no hidden spec in the bundle", "cost_usd": 0.0})
    instruction = (bundle_dir / "instruction.md").read_text()

    review_dir = leak_review_dir(bundle_dir, trace_dir)
    record_path = review_dir / "review.json"
    if record_path.exists():
        record = json.loads(record_path.read_text())
        record["adopted"], record["cost_usd"] = True, 0.0
    else:
        from shared.llm import llm  # shared/llm/llm.py
        messages = [{"role": "system", "content": LEAK_SYSTEM},
                    {"role": "user", "content": f"HIDDEN AUTHOR NOTES\n\n{notes}\n\n"
                                                f"INSTRUCTION SHOWN TO THE AGENT\n\n{instruction}"}]
        completion = llm.complete(messages, LEAK_FORMAT, llm_dir=review_dir / "llm", cache_key="leak-review")
        try:
            review = json.loads(completion.content)
        except json.JSONDecodeError:
            review = {"leaks": [], "verdict": "unparseable"}
        record = {"review": review, "request_sha256": completion.request_sha256,
                  "cost_usd": 0.0 if completion.cached else completion.cost_usd, "adopted": False}
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, indent=1) + "\n")

    review = record["review"]
    if review.get("verdict") == "unparseable":
        raise RuntimeError("leak review returned unparseable JSON")
    haystack = _normalized(instruction)
    confirmed = [x for x in review.get("leaks", []) if x.get("quote") and _normalized(x["quote"]) in haystack]
    unverified = [x for x in review.get("leaks", []) if x not in confirmed]
    detail = {"verdict": review.get("verdict"), "leaks": confirmed, "unverified_quotes": unverified,
              "request_sha256": record.get("request_sha256"), "cost_usd": record["cost_usd"],
              "adopted": record.get("adopted", False)}
    if not confirmed:
        return Gate("leak", True, detail)
    listed = "\n".join(f"- \"{x['quote']}\"\n  reveals: {x['reveals']}" for x in confirmed)
    feedback = ("A reviewer compared the instruction with your hidden spec and found sentences that give "
                f"away the root cause or the fix:\n{listed}\n\nRewrite them so the instruction states what "
                "must be true in terms of the system's interface (commands, inputs, outputs, files), without "
                "naming the faulty code or describing the change. Keep every requirement the tests check, and "
                "do not single out the exact cases the defect breaks.")
    return Gate("leak", False, detail, feedback)


def run_validate_job(bundle_dir: str | Path, trace_dir: str | Path) -> dict:
    """Run the gates on one bundle.

    Returns {outcome, failed_gate, result: {gates}, feedback, cost_usd}. Only the
    leak review costs money; its spend is cost_usd (0 when adopted on a retry).
    """
    bundle_dir, trace_dir = Path(bundle_dir), Path(trace_dir)
    steps = [
        lambda: gate_structural(bundle_dir),
        lambda: gate_lint(bundle_dir),
        lambda: gate_oracle(bundle_dir, trace_dir, "oracle1"),
        lambda: gate_oracle(bundle_dir, trace_dir, "oracle2"),
        lambda: gate_nop(bundle_dir, trace_dir),
        lambda: gate_wrong_fix(bundle_dir, trace_dir),
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
        sys.exit("Usage: python gates.py <bundle_dir> <trace_dir>")
    res = run_validate_job(sys.argv[1], sys.argv[2])
    for name, g in res["result"]["gates"].items():
        print(f"{'PASS' if g['passed'] else 'FAIL'}  {name}")
    if res["feedback"]:
        print("\n" + res["feedback"])
    sys.exit(0 if res["outcome"] == "OK" else 1)
