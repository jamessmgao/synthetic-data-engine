"""
Trace parser: why did a rollout fail? (docs/PRD.md §6.5)

Two layers, in this order:

1. **Deterministic extraction** (every valid rollout, no model call):
   - ``trace_metrics`` from the agent trajectory (Harbor's ``agent/trajectory.json``)
     and the trial result: LLM calls, tokens, the last tool calls with their
     terminal output, files the agent's commands wrote to, duration.
   - for a VALID_FAIL, numbered **evidence** lines: the failing tests and their
     assertion messages (``verifier/ctrf.json``, falling back to
     ``test-stdout.txt``), the agent's last commands with output, its final
     message and whether it declared the task complete.

2. **LLM review** (VALID_FAIL only, one GLM-5.1 call): given the evidence, the
   task instruction and the task's hidden spec (root cause, intended pitfalls),
   return an explanation, an attribution (``model``: the agent got it wrong;
   ``task``: the task is defective) and which intended pitfall, if any, it fell
   into. The explanation must cite evidence ids. An explanation that cites
   nothing real, or a failed call, is recorded as attribution ``unexplained``:
   kept, never trusted, never blocking.

The result goes onto the rollout's own job row (``failure``, ``trace_metrics``).
The Trace Parser service (evaluation/trace_parser_service.py) calls ``parse`` on
every scored run of a version before it decides, so an accepted task's failures
are always explained. The GLM call is cached per run, so a reclaimed analysis
reuses the explanation instead of paying again.

Re-parse settled rows (audit or backfill) with:

    python -m evaluation.trace_parser [--job JOB_ID] [--all-missing]
"""

import argparse
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Callable

from evaluation.classify import VALID_FAIL, parse_time

log = logging.getLogger("trace_parser")

MAX_TOOL_CALLS = 20  # kept in trace_metrics
MAX_EVIDENCE_COMMANDS = 8  # agent commands quoted as evidence
MAX_ASSERTION_LINES = 6  # per failing test
CMD_CHARS, OUTPUT_CHARS, MESSAGE_CHARS = 300, 500, 800
UNEXPLAINED = "unexplained"
REVIEW_ATTEMPTS = 3

# --- deterministic extraction ----------------------------------------------------


def load_trajectory(trial_dir: Path) -> dict | None:
    path = trial_dir / "agent" / "trajectory.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _agent_steps(trajectory: dict | None) -> list[dict]:
    return [s for s in (trajectory or {}).get("steps", []) if s.get("source") == "agent"]


def _tool_calls(trajectory: dict | None) -> list[dict]:
    """Every tool call in order, paired with the terminal output it produced."""
    calls = []
    for step in _agent_steps(trajectory):
        outputs = {r.get("source_call_id"): r.get("content", "")
                   for r in ((step.get("observation") or {}).get("results") or [])}
        for call in step.get("tool_calls") or []:
            args = call.get("arguments") or {}
            calls.append({
                "tool": call.get("function_name"),
                "input": args.get("keystrokes", "") if isinstance(args, dict) else str(args),
                "output": outputs.get(call.get("tool_call_id"), ""),
            })
    return calls


_WRITE_PATTERNS = [
    re.compile(r"(?<![0-9&<>])>>?\s*([^\s;|&<>()'\"]+)"),  # cmd > path, cmd >> path, cat > path <<EOF
    re.compile(r"\btee\s+(?:-a\s+)?([^\s;|&<>]+)"),
    re.compile(r"\bsed\s+-i\S*\s+(?:'[^']*'|\"[^\"]*\"|\S+)\s+([^\s;|&<>]+)"),
    re.compile(r"open\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"][wa]"),
    re.compile(r"\b(?:gofmt|goimports|black|rustfmt|prettier)\s+(?:-\S+\s+)*-w\s+([^\s;|&<>]+)"),
]
# Python heredocs: path = Path('x') ... path.write_text(...) writes x.
_PY_PATH = re.compile(r"Path\(\s*['\"]([^'\"]+)['\"]\s*\)")
# cp/mv/install as the command itself (not `apt-get install pkg`); the destination is the last argument.
_COPY_COMMAND = re.compile(r"(?:^|[;&|(]\s*|\bsudo\s+)(?:cp|mv|install)\s+([^;|&\n]+)", re.M)


def files_modified(commands: list[str]) -> list[str]:
    """Paths the agent's commands wrote to. A heuristic over the command text:
    it cannot see writes made by programs the agent ran."""
    found = set()
    for cmd in commands:
        paths = [p for pattern in _WRITE_PATTERNS for p in pattern.findall(cmd)]
        paths += [args.split()[-1] for args in _COPY_COMMAND.findall(cmd) if len(args.split()) >= 2]
        if re.search(r"\.write_(?:text|bytes)\(", cmd):
            paths += _PY_PATH.findall(cmd)
        for path in paths:
            if path.startswith(("/dev/", "&", "$", "-")) or path == "":
                continue
            found.add(path.rstrip(";"))
    return sorted(found)


def trace_metrics(trial_dir: Path, result: dict) -> dict:
    trajectory = load_trajectory(trial_dir)
    calls = _tool_calls(trajectory)
    agent = result.get("agent_result") or {}
    timing = result.get("agent_execution") or {}
    duration = None
    if timing.get("started_at") and timing.get("finished_at"):
        duration = (parse_time(timing["finished_at"]) - parse_time(timing["started_at"])).total_seconds()
    return {
        "llm_calls": sum(1 for s in _agent_steps(trajectory) if s.get("metrics")),
        "input_tokens": agent.get("n_input_tokens"),
        "output_tokens": agent.get("n_output_tokens"),
        "tool_calls": [{"tool": c["tool"], "input": c["input"][:CMD_CHARS],
                        "output": c["output"][-OUTPUT_CHARS:], "exit_code": None}
                       for c in calls[-MAX_TOOL_CALLS:]],
        # None when there is no trajectory: unknown, not "touched nothing".
        "files_modified": files_modified([c["input"] for c in calls]) if trajectory is not None else None,
        "duration_seconds": duration,
        "declared_complete": any(c["tool"] == "mark_task_complete" for c in calls),
    }


def _assertion_lines(trace: str) -> list[str]:
    lines = [ln.strip() for ln in trace.splitlines() if ln.lstrip().startswith(("E ", "E\t", "assert"))]
    return [ln[:300] for ln in lines[:MAX_ASSERTION_LINES]]


def failing_tests(trial_dir: Path) -> list[dict]:
    """[{name, message, assertions}] from the verifier; empty if nothing failed or no output."""
    verifier = trial_dir / "verifier"
    try:
        ctrf = json.loads((verifier / "ctrf.json").read_text())
        return [{"name": t.get("name"), "message": (t.get("message") or "")[:300],
                 "assertions": _assertion_lines(t.get("trace") or "")}
                for t in ctrf.get("results", {}).get("tests", []) if t.get("status") == "failed"]
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    try:
        stdout = (verifier / "test-stdout.txt").read_text(errors="replace")
    except OSError:
        return []
    failed = re.findall(r"^FAILED\s+(\S+)(?:\s+-\s+(.*))?$", stdout, flags=re.M)  # pytest -rA summary
    assertions = _assertion_lines(stdout)
    return [{"name": n, "message": m.strip()[:300], "assertions": assertions if i == 0 else []}
            for i, (n, m) in enumerate(failed)]


def extract_evidence(trial_dir: Path) -> tuple[list[dict], list[dict]]:
    """(failing tests, numbered evidence lines) for a failed rollout."""
    tests = failing_tests(trial_dir)
    evidence: list[dict] = []

    def add(source: str, text: str) -> None:
        evidence.append({"id": f"E{len(evidence) + 1}", "source": source, "text": text})

    for t in tests:
        add("verifier", f"FAILED {t['name']}" + (f" - {t['message']}" if t["message"] else ""))
        for line in t["assertions"]:
            add("verifier", line)
    trajectory = load_trajectory(trial_dir)
    calls = _tool_calls(trajectory)
    for c in calls[-MAX_EVIDENCE_COMMANDS:]:
        if c["tool"] == "mark_task_complete":
            add("agent", "agent declared the task complete")
            continue
        out = c["output"].strip()[-OUTPUT_CHARS:]
        add("agent", f"$ {c['input'].strip()[:CMD_CHARS]}" + (f"\n→ {out}" if out else ""))
    steps = _agent_steps(trajectory)
    if steps and steps[-1].get("message"):
        add("agent", f"final message: {steps[-1]['message'].strip()[:MESSAGE_CHARS]}")
    if trajectory is None:
        add("agent", "no agent trajectory was recorded")
    return tests, evidence


def load_spec(work_dir: Path, bundle_sha256: str | None) -> dict | None:
    """The task's hidden spec from the generator's draft record, if this bundle was generated."""
    if not bundle_sha256:
        return None
    try:
        record = json.loads((work_dir / "drafts" / f"{bundle_sha256}.json").read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return (record.get("draft") or {}).get("spec")


def load_instruction(bundle_dir: Path | None) -> str:
    if bundle_dir is None:
        return ""
    try:
        return (bundle_dir / "instruction.md").read_text(errors="replace")[:4000]
    except OSError:
        return ""


# --- LLM review --------------------------------------------------------------------

REVIEW_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "failure_review",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            "required": ["explanation", "attribution", "pitfall_matched", "cited_evidence"],
            "properties": {
                "explanation": {"type": "string",
                                "description": "2-4 sentences: what the agent did wrong, citing evidence ids."},
                "attribution": {"type": "string", "enum": ["model", "task"],
                                "description": "model: the agent made a mistake on a fair task. task: the task is "
                                               "defective (ambiguous or contradictory instruction, broken "
                                               "environment, a test checking something the instruction never asked)."},
                "pitfall_matched": {"type": "string",
                                    "description": "One of the spec's expected_agent_pitfalls, copied exactly, if the "
                                                   "agent fell into it; otherwise exactly 'none'. Explain mismatches "
                                                   "in the explanation, not here."},
                "cited_evidence": {"type": "array", "items": {"type": "string"},
                                   "description": "Evidence ids (E1, E2, ...) that support the explanation."},
            },
        },
    },
}

REVIEW_PROMPT = """You review why an AI coding agent failed a terminal task. Work only from
the evidence below; do not invent facts. Cite evidence ids for every claim.

Decide the attribution carefully: "task" only if the evidence shows the task
itself is unfair (the instruction is ambiguous or contradicts the tests, the
environment is broken, or a test checks something the instruction never asked
for). Otherwise "model".

## Task instruction (what the agent saw)
{instruction}

## Hidden task spec (the agent never saw this)
{spec}

## Evidence
{evidence}
"""


def _llm():
    """shared/llm/llm.py uses flat imports (`import config`), so put generator/ on the path."""
    root = Path(__file__).resolve().parent.parent
    if str(root / "generator") not in sys.path:
        sys.path.insert(0, str(root / "generator"))
    from shared.llm import llm
    return llm


Complete = Callable[..., object]  # shared.llm.llm.complete


def _match_pitfall(answer: str | None, spec: dict | None) -> str | None:
    """The spec pitfall the reviewer named, or None. Anything that isn't one of the
    spec's pitfalls (prose, 'none', an invented trap) counts as no match."""
    pitfalls = (spec or {}).get("expected_agent_pitfalls") or []
    answer = (answer or "").strip().strip("'\"").lower()
    return next((p for p in pitfalls if p.strip().lower() == answer), None)


def review(evidence: list[dict], instruction: str, spec: dict | None, *, cache_key: str,
           llm_dir: Path | None, complete: Complete | None = None) -> tuple[dict, float]:
    """One GLM-5.1 review of a failure. Returns (review fields, cost in USD). Never raises."""
    spec_text = json.dumps({k: spec.get(k) for k in ("root_cause", "correctness_constraints",
                                                    "expected_agent_pitfalls", "failure_scenario")},
                           indent=1) if spec else "(none: this task has no recorded spec)"
    prompt = REVIEW_PROMPT.format(
        instruction=instruction or "(not available)",
        spec=spec_text,
        evidence="\n".join(f"[{e['id']}] ({e['source']}) {e['text']}" for e in evidence),
    )
    out, cost, error = None, 0.0, None
    for attempt in range(1, REVIEW_ATTEMPTS + 1):
        # GLM-5.1 sometimes aborts a strict-JSON response upstream ("model output
        # became abnormal"); a plain retry usually succeeds. Only finished calls
        # are cached, so a retry is a new call.
        try:
            complete = complete or _llm().complete
            c = complete([{"role": "user", "content": prompt}], REVIEW_SCHEMA, llm_dir=llm_dir, cache_key=cache_key)
            cost += 0.0 if getattr(c, "cached", False) else float(getattr(c, "cost_usd", 0.0) or 0.0)
            out = json.loads(c.content)
            break
        except Exception as e:  # gateway error, bad JSON, ...: retry, then record; never block the rollout
            error = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("%s: failure review attempt %d/%d failed: %s", cache_key, attempt, REVIEW_ATTEMPTS, error)
    if out is None:
        return {"explanation": None, "attribution": UNEXPLAINED, "pitfall_matched": None,
                "cited_evidence": [], "review_error": error}, cost

    known = {e["id"] for e in evidence}
    cited = [i for i in out.get("cited_evidence", []) if i in known]
    attribution = out.get("attribution") if out.get("attribution") in ("model", "task") else UNEXPLAINED
    if not cited:
        attribution = UNEXPLAINED  # an explanation that cites nothing real is not trusted
    return {"explanation": out.get("explanation"), "attribution": attribution,
            "pitfall_matched": _match_pitfall(out.get("pitfall_matched"), spec),
            "cited_evidence": cited}, cost


# --- entry point used by the executor --------------------------------------------------


def parse(trial_dir: Path, result: dict, outcome: str, *, job_id: str, work_dir: Path,
          bundle_sha256: str | None = None, bundle_dir: Path | None = None,
          complete: Complete | None = None) -> tuple[dict, dict | None, float]:
    """(trace_metrics, failure or None, review cost) for one valid rollout."""
    metrics = trace_metrics(trial_dir, result)
    if outcome != VALID_FAIL:
        return metrics, None, 0.0
    tests, evidence = extract_evidence(trial_dir)
    verdict, cost = review(evidence, load_instruction(bundle_dir), load_spec(work_dir, bundle_sha256),
                           cache_key=f"trace-review:{job_id}", llm_dir=work_dir / "llm", complete=complete)
    failure = {"failing_tests": [t["name"] for t in tests], "evidence": evidence, **verdict}
    return metrics, failure, cost


# --- backfill / audit CLI -------------------------------------------------------------------


def _reparse(backend, job: dict) -> bool:
    """Parse one settled rollout and write it if its failure is still missing (compare-and-set)."""
    trace = backend.artifacts.trace_dir(job["id"])
    trial = (job.get("result") or {}).get("trial")
    trial_dir = trace / trial if trial else next((p.parent for p in trace.glob("*/result.json")), None)
    if trial_dir is None or not (trial_dir / "result.json").exists():
        log.warning("%s: no local trace to parse", job["id"])
        return False
    bundle = backend.artifacts.bundle_path(job["bundle_sha256"]) if job.get("bundle_sha256") else None
    metrics, failure, cost = parse(trial_dir, json.loads((trial_dir / "result.json").read_text()),
                                   job["outcome"], job_id=job["id"], work_dir=backend.work_dir,
                                   bundle_sha256=job.get("bundle_sha256"), bundle_dir=bundle)
    with backend.store.tx() as c:
        cur = c.execute(
            "UPDATE jobs SET trace_metrics = ?, failure = ?, cost_usd = COALESCE(cost_usd, 0) + ?"
            " WHERE id = ? AND status = 'DONE' AND failure IS NULL",
            (json.dumps(metrics), json.dumps(failure) if failure else None, cost, job["id"]),
        )
        return cur.rowcount == 1


def main() -> None:
    from shared.storage import from_env

    parser = argparse.ArgumentParser(description="Re-parse settled rollouts (audit or backfill).")
    parser.add_argument("--job", action="append", default=[], help="job id (repeatable)")
    parser.add_argument("--all-missing", action="store_true",
                        help="every DONE VALID_FAIL rollout without a failure record")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    backend = from_env()
    ids = list(args.job)
    if args.all_missing:
        ids += [r["id"] for r in backend.store.conn.execute(
            "SELECT id FROM jobs WHERE type = 'rollout' AND status = 'DONE' AND outcome = 'VALID_FAIL'"
            " AND failure IS NULL").fetchall()]
    for job_id in ids:
        job = backend.store.job(job_id)
        wrote = job is not None and job["outcome"] in ("VALID_PASS", "VALID_FAIL") and _reparse(backend, job)
        print(f"{job_id}: {'written' if wrote else 'skipped (missing, not valid, or already parsed)'}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
