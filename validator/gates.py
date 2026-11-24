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
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

if not __package__:  # run by path (python validator/gates.py): the repo root goes on the import path, in place of this directory
    _here = Path(__file__).resolve().parent
    sys.path[:] = [str(_here.parent)] + [p for p in sys.path if p != str(_here)]

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
        try:
            subprocess.run(
                [config.HARBOR_BIN, "run", "-p", str(bundle_dir), "-a", agent, "-o", str(trace_dir),
                 "--job-name", step, "-y", "-q"],
                capture_output=True, text=True, timeout=config.GATE_TIMEOUT_SEC,
            )
        finally:
            # A killed, crashed or timed-out Harbor never runs compose down: its trial
            # network leaks, and leaked networks exhausted Docker's address pools.
            from generator.authoring_harness import remove_trial_networks

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


# --- leak review ---------------------------------------------------------------
# Up to three glm-5.1 calls after the functional gates (or at create time, agent
# mode): does the instruction the agent reads give away the fix or the failing
# sequence, in any words? In the pilots, prompt rules alone did not stop this
# (pipeline-hard, h6; P1's STOP_AFTER hook), and it made tasks too easy.

LEAK_SYSTEM = """You review a benchmark task before an AI agent attempts it. You get the
author's hidden notes (root cause, solution subgoals, correctness constraints), the
world the task was written from when there is one (its counterexample, invariants,
plausible wrong fix and the real fix), the reference solution, and the instruction
the agent will read. Decide whether the instruction gives away the root cause, the
fix or the failing sequence, literally OR SEMANTICALLY.

Go through the instruction sentence by sentence and ask: does this sentence,
paraphrased, tell the agent what the fix does, where it goes, which mechanism to
use, or which sequence breaks the system? Synonyms and paraphrases count: "reuse
the prior durable result for that request ID" is "make the effect idempotent on the
request ID". Then ask of the whole instruction: could an agent reproduce the
counterexample, or arrive at the fix, from the instruction alone, without reading
and reasoning about the code? If so, the sentences that make it possible are leaks.

A LEAK is a sentence of the instruction that tells the agent where the defect
is or what change to make. For example, it:
- names the faulty file, function, class, variable, field or mechanism;
- describes how the internal code currently misbehaves, rather than what the
  user observes;
- states a requirement in words that amount to the patch (for example "the
  check must compare against totals computed independently from raw events"
  when the bug is that it compares against the wrong totals, or "any later
  delivery of that request ID must reuse that exact durable result" when the
  fix is to key the effect on the request ID);
- singles out exactly the cases the planted defects break (for example
  "including transactions with three or more entries" when the bug drops the
  third entry);
- describes the failing scenario itself: the sequence of steps, the schedule or
  interleaving, where a crash or fault hook fires relative to the system's effects
  (for example "the worker exits after the inventory update is durable and before
  its remaining completion work"), or the exact input or time step that triggers
  it (for example "use tick --milliseconds 30001 to expire the lease" when that
  expiry is the counterexample). Discovering the failing sequence is the task.

A crash hook or stage list in the instruction (an env var that stops the process
at a named stage) is a leak: it maps out where the failure is. Documenting a clock
or other control command by its interface is not a leak; saying which use of it
breaks the system is.

NOT a leak:
- user-visible symptoms;
- required observable behavior of commands, outputs and files, even when that
  behavior is what is currently broken;
- input and output formats, and names of files, commands or fields that are
  part of the interface the tests check;
- general rules that cover the broken case among many others.

Flag only real leaks. For each, quote the sentence exactly as it appears in the
instruction, say which part of the fix or the counterexample it reveals, and grade
it: "direct" (it states the fix, its location, its mechanism or the failing
sequence, in any words) or "strong_hint" (it narrows the search to it without
stating it). Return verdict "leak" if there is a direct finding or two or more
strong hints; otherwise "clean" (list any single strong hint anyway)."""

# The second, independent check: a reviewer without the hidden notes predicts the fix
# from the instruction alone; a third call judges the prediction against the real fix.
BLIND_SYSTEM = """You are a strong software engineer. You get only a task's instruction,
not its code. Predict, as specifically as the instruction lets you: (1) the change
that fixes the problem: its mechanism and where it goes, and (2) the sequence of
events that makes the system fail today. If the instruction does not let you tell,
say so instead of guessing a generic answer. Put your whole answer inside the two
fields, in a few sentences each; never refer to text outside them."""

# Under the gateway's strict-schema constrained decoding an unescaped " ends a string field
# early and the answer still parses (evaluation/trace_parser.py NO_DOUBLE_QUOTES).
_PROSE = {"type": "string", "description": "Quote with single quotes, never double quotes."}

BLIND_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "blind_prediction", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["predicted_fix", "predicted_failure"],
        "properties": {"predicted_fix": _PROSE, "predicted_failure": _PROSE}}},
}

JUDGE_SYSTEM = """You compare an engineer's prediction, made from a task's instruction
alone, with the task's real fix and counterexample. Answer "specific" only if the
prediction names the real fix's mechanism AND where it applies, so that the agent would
not need to investigate the code to find it. Predicting the failing sequence counts only
together with the right location; the textbook failure for the guarantee stated (e.g. a
write acknowledged before it is durable, for "acknowledged writes survive failover") is
"generic".
Answer "generic" if it is only a reasonable general remedy any careful engineer
would list (add validation, make it transactional, add retries), and "none" if it
does not match. Both parts must hold for "specific": a right mechanism in the wrong
module, file or path is "generic". The textbook remedies for the guarantee stated
(an idempotency key, a lock, fsync or an atomic write for an exactly-once or
crash-safety guarantee) are "generic" unless the prediction also names where the real
fix applies. A prediction that is, or includes as its remedy, the task's plausible
wrong fix is not "specific"."""

JUDGE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "blind_judgement", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["match", "why"],
        "properties": {"match": {"type": "string", "enum": ["specific", "generic", "none"]},
                       "why": _PROSE}}},
}

LEAK_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "leak_review", "strict": True, "schema": {
        "type": "object", "additionalProperties": False, "required": ["leaks", "verdict"],
        "properties": {
            "leaks": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["quote", "reveals", "severity"],
                "properties": {"quote": {"type": "string"}, "reveals": _PROSE,  # quote: exact text
                               "severity": {"type": "string", "enum": ["direct", "strong_hint"]}}}},
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


CONTEXT_CHARS = 12_000  # per hidden file shown to the reviewer


def _world_dir(bundle_dir: Path) -> Path | None:
    """The world a content-addressed bundle was written from (task.toml's generation
    world_sha256, under <work>/worlds/), if it is here."""
    import tomllib

    try:
        meta = tomllib.loads((bundle_dir / "task.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return None
    sha = ((meta.get("metadata") or {}).get("generation") or {}).get("world_sha256")
    if not sha or bundle_dir.parent.name != "bundles":
        return None
    world = bundle_dir.parent.parent / "worlds" / sha
    return world if (world / "world.json").is_file() else None


def _world_context(bundle_dir: Path) -> str:
    """The world's counterexample, invariants, wrong fix and real fix, for the reviewer."""
    world = _world_dir(bundle_dir)
    if world is None:
        return ""
    meta = json.loads((world / "world.json").read_text())
    ce = meta.get("counterexample") or {}
    parts = ["## World invariants", *(f"- {i}" for i in meta.get("invariants") or []),
             "", "## Counterexample (the failing sequence)", *(f"{n}. {s}" for n, s in
                                                              enumerate(ce.get("sequence") or [], 1)),
             f"Observed: {ce.get('observed', '')}", "", f"## Plausible wrong fix\n{meta.get('plausible_wrong_fix', '')}"]
    if (fix := world / "world_fix" / "fix.sh").is_file():
        parts += ["", "## The real fix (world_fix/fix.sh)", fix.read_text()[:CONTEXT_CHARS]]
    defeats = [m.get("defeats") for m in ((meta.get("lineage") or {}).get("mutations") or [])
               if isinstance(m, dict) and m.get("defeats")]
    if defeats:  # aws-agent-b1-t04 v2: a prediction of only the parent's (already solved) fix was judged a leak
        parts += ["", "## This world is a mutation of a solved parent",
                  "Agents already solved the parent's defect, so the parent's part of the fix is known: a "
                  "prediction that names only that part is generic, not specific. What this version adds:",
                  *(f"- {d}" for d in defeats)]
    return "\n".join(parts)


def _solution_context(bundle_dir: Path) -> str:
    """The reference solution (solution/, the wrong fix left out)."""
    sol = bundle_dir / "solution"
    if not sol.is_dir():
        return ""
    files = [p for p in sorted(sol.rglob("*")) if p.is_file() and p.name != "wrong_fix.sh"]
    return "\n\n".join(f"## solution/{p.relative_to(sol)}\n{p.read_text(errors='replace')[:CONTEXT_CHARS]}"
                       for p in files)


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
    """Reject a bundle whose instruction reveals the fix or the failing sequence.

    Three calls (_leak_calls): the semantic review (hidden notes + the world + the
    solution; findings graded direct / strong_hint; a leak is any direct or 2+ strong
    hints), then, unless SDE_LEAK_BLIND=0, a blind fix prediction from the instruction
    alone and its judgement against the real fix (only a "specific" match is a leak).
    The record is stored at leak_review_dir()/review.json and adopted on retry and by
    any later job on the same bundle. A finding only counts if its quote really appears
    in the instruction, so an over-eager or hallucinating reviewer cannot fail a task
    on its own words.
    """
    if not config.LEAK_REVIEW:
        return Gate("leak", True, {"skipped": "disabled (SDE_LEAK_REVIEW=0)", "cost_usd": 0.0})
    readme = bundle_dir / "README.md"
    notes = _hidden_notes(readme.read_text()) if readme.exists() else ""
    if not notes:
        return Gate("leak", True, {"skipped": "no hidden spec in the bundle", "cost_usd": 0.0})
    instruction = (bundle_dir / "instruction.md").read_text()
    hidden = "\n\n".join(filter(None, [f"HIDDEN AUTHOR NOTES\n\n{notes}",
                                       (w := _world_context(bundle_dir)) and f"THE WORLD\n\n{w}",
                                       (s := _solution_context(bundle_dir)) and f"REFERENCE SOLUTION\n\n{s}"]))

    review_dir = leak_review_dir(bundle_dir, trace_dir)
    record_path = review_dir / "review.json"
    record = json.loads(record_path.read_text()) if record_path.exists() else None
    if record is not None and record.get("version") == LEAK_RECORD_VERSION:
        record["adopted"], record["cost_usd"] = True, 0.0
    else:  # none yet, or one from before the blind check: review again
        record = _leak_calls(review_dir, hidden, instruction)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, indent=1) + "\n")

    review = record["review"]
    if review.get("verdict") == "unparseable":  # a record from before inconclusive existed
        raise RuntimeError("leak review returned unparseable JSON")
    inconclusive = list(record.get("inconclusive") or [])
    haystack = _normalized(instruction)
    confirmed = [x for x in review.get("leaks", []) if x.get("quote") and _normalized(x["quote"]) in haystack]
    unverified = [x for x in review.get("leaks", []) if x not in confirmed]
    direct = [x for x in confirmed if x.get("severity", "direct") == "direct"]  # ungraded = direct, as before
    hints = [x for x in confirmed if x.get("severity") == "strong_hint"]
    blind = record.get("blind") or {}
    blind_leak = (blind.get("judge") or {}).get("match") == "specific"
    cut = [name for name, text in (("blind.prediction.predicted_fix", (blind.get("prediction") or {}).get("predicted_fix")),
                                   ("blind.judge.why", (blind.get("judge") or {}).get("why")))
           if text and re.search(r"(?:\s|[=:(,])$", text)]  # an unescaped " ended it: 'status '
    detail = {"verdict": review.get("verdict"), "leaks": confirmed, "unverified_quotes": unverified,
              "direct": len(direct), "strong_hints": len(hints), "blind": blind, "blind_leak": blind_leak,
              "cut_fields": cut,
              "had_world": "THE WORLD" in hidden, "calls": record.get("calls", []),
              "request_sha256": record.get("request_sha256"), "cost_usd": record["cost_usd"],
              "adopted": record.get("adopted", False), "inconclusive": inconclusive,
              "blind_inconclusive": "blind" in inconclusive}
    review_leak = bool(direct) or len(hints) >= 2
    if not review_leak and not blind_leak:
        if inconclusive and config.LEAK_INCONCLUSIVE == "fail":
            return Gate("leak", False, detail, (
                f"The leak review was inconclusive ({', '.join(inconclusive)}: no parseable answer after a retry, "
                "or every attempt timed out), and SDE_LEAK_INCONCLUSIVE=fail. Nothing in the instruction was "
                "found to leak; the review needs to be run again."))
        if inconclusive:
            print(f"leak review inconclusive ({', '.join(inconclusive)}) for {bundle_dir}: passed "
                  "(SDE_LEAK_INCONCLUSIVE=warn); see detail.inconclusive", file=sys.stderr, flush=True)
        return Gate("leak", True, detail)
    parts = []
    if review_leak:
        listed = "\n".join(f"- \"{x['quote']}\" ({x.get('severity', 'direct')})\n  reveals: {x['reveals']}"
                           for x in [*direct, *hints])
        parts.append("A reviewer compared the instruction with the hidden fix and counterexample and found "
                     f"sentences that give them away, literally or in other words:\n{listed}")
    if blind_leak:
        parts.append("A second reviewer who saw ONLY the instruction predicted the real fix:\n"
                     f"  predicted: {(blind.get('prediction') or {}).get('predicted_fix', '')}\n"
                     f"  judged a specific match: {blind['judge'].get('why', '')}")
    feedback = ("\n\n".join(parts) + "\n\nRewrite the instruction so it states what must be true through the "
                "system's interface (commands, inputs, outputs, files) and the symptom users saw, without the "
                "fix's mechanism or location, the failing sequence, crash hooks or stage names. Keep every "
                "requirement the tests check, and do not single out the exact cases the defect breaks. "
                "Remove sentences, never add explanations: a sentence about how the system stores, recovers "
                "or computes something makes the leak worse.")
    return Gate("leak", False, detail, feedback)


LEAK_RECORD_VERSION = 4  # 3: a placeholder blind prediction ("...") is retried, then inconclusive; 4: pointers too

_POINTERS = ("see below", "see above", "see detailed", "as above", "as below", "detailed answer below")


def _placeholder(text) -> bool:
    """A model answer with no content: "", "...", "…", or a pointer to text outside the field ("See
    detailed answer below."). aws-agent-b1: such blind predictions were judged "none" and passed the gate."""
    if not isinstance(text, str):
        return True
    words = "".join(ch for ch in text if ch.isalnum() or ch == " ")
    return len(words.replace(" ", "")) < 4 or (len(text) < 80 and any(p in text.lower() for p in _POINTERS))


def _leak_calls(review_dir: Path, hidden: str, instruction: str) -> dict:
    """The three calls: the semantic review, the blind prediction, its judgement. Each is
    recorded (llm/) with its cost."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from shared.llm import llm  # shared/llm/llm.py

    calls: list[dict] = []
    lock = threading.Lock()

    def once(system: str, user: str, fmt: dict, key: str) -> dict | None:
        """One call (timeout + retries); its parsed JSON, or None if unparseable or timed out."""
        try:
            completion = llm.complete([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                      fmt, llm_dir=review_dir / "llm", cache_key=key,
                                      timeout=config.LEAK_CALL_TIMEOUT_S,
                                      timeout_retries=config.LEAK_CALL_TIMEOUT_RETRIES)
        except llm.CallTimeout as e:
            with lock:
                calls.append({"call": key, "request_sha256": None, "cost_usd": 0.0, "error": str(e)})
            return None
        spent = 0.0 if completion.cached else completion.cost_usd
        with lock:
            calls.append({"call": key, "request_sha256": completion.request_sha256, "cost_usd": spent})
        try:
            return json.loads(completion.content)
        except json.JSONDecodeError:
            return None

    def call(system: str, user: str, fmt: dict, key: str, required: str) -> dict | None:
        """The answer, retried once (a new cache key) when unparseable, missing `required`, or
        a placeholder ("...") in place of `required`'s text."""
        for attempt in (key, f"{key}-retry1"):
            answer = once(system, user, fmt, attempt)
            if answer is not None and required in answer and not _placeholder(answer[required]):
                return answer
        return None

    review_user = f"{hidden}\n\nINSTRUCTION SHOWN TO THE AGENT\n\n{instruction}"
    inconclusive: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as pool:  # the review and the blind prediction are independent
        review_f = pool.submit(call, LEAK_SYSTEM, review_user, LEAK_FORMAT, "leak-review", "verdict")
        prediction_f = (pool.submit(call, BLIND_SYSTEM, f"INSTRUCTION\n\n{instruction}", BLIND_FORMAT, "leak-blind",
                                    "predicted_fix") if config.LEAK_BLIND else None)
        review, prediction = review_f.result(), prediction_f.result() if prediction_f else None
    if review is None:
        inconclusive.append("review")
        review = {"leaks": [], "verdict": "inconclusive"}
    if not config.LEAK_BLIND:
        blind = {"skipped": "SDE_LEAK_BLIND=0"}
    elif prediction is None:
        inconclusive.append("blind")
        blind = {"prediction": None, "judge": None, "inconclusive": True}
    else:
        judge = call(JUDGE_SYSTEM, f"{hidden}\n\nPREDICTION FROM THE INSTRUCTION ALONE\n\n"
                                   f"Fix: {prediction.get('predicted_fix', '')}\n"
                                   f"Failure: {prediction.get('predicted_failure', '')}", JUDGE_FORMAT,
                     "leak-judge", "match")
        if judge is None:
            inconclusive.append("blind")
        blind = {"prediction": prediction, "judge": judge, **({"inconclusive": True} if judge is None else {})}
    order = {"leak-review": 0, "leak-review-retry1": 1, "leak-blind": 2, "leak-blind-retry1": 3,
             "leak-judge": 4, "leak-judge-retry1": 5}
    calls.sort(key=lambda c: order.get(c["call"], 9))
    return {"version": LEAK_RECORD_VERSION, "review": review, "blind": blind, "inconclusive": inconclusive,
            "calls": calls, "request_sha256": next((c["request_sha256"] for c in calls if c["request_sha256"]), None),
            "cost_usd": round(sum(c["cost_usd"] for c in calls), 6), "adopted": False}


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
