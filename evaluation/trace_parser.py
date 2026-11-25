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

3. **Pass review** (a version that came in too easy, one GLM-5.1 call per
   version, over its fastest passing runs; ``review_passes``): why the agents
   passed so easily, the simple strategy that worked, the cues in the task that
   led them to it, the fix they applied, and up to three semantic changes (named
   from ``shared.contracts.hardening.HARDENING_TECHNIQUES``) that would defeat that strategy. The
   Trace Parser service puts it into the revision job, so the next version is
   hardened against what the agents actually did. Untrusted (cites no real
   evidence) or failed reviews are recorded and left out of the revision prompt.

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


# What counts as a file the agent changed: an absolute path outside the scratch dirs (config
# tasks edit /etc, hooks, /usr/local/bin), or a relative path that names a file (an extension
# or a known name). Not a token a `>` inside heredoc code captured ("y", "Type,", "="), and not
# a backup or scratch output.
_ABSOLUTE = re.compile(r"(?:/[\w.@+-]+){2,}")
_RELATIVE = re.compile(r"(?:[\w.-]+/)*(?:[\w-][\w.-]*\.\w{1,8}|Makefile|Dockerfile)")
_SCRATCH_DIRS = ("/tmp/", "/var/tmp/", "/proc/", "/dev/", "/logs/", "/run/")
_BACKUP = re.compile(r"\.(?:orig|bak|old|rej|swp|pid)$|~$")


def files_modified(commands: list[str]) -> list[str]:
    """Files the agent's commands wrote to: /app's (the working directory) relative to it,
    others absolute. A heuristic over the command text: it cannot see writes made by
    programs the agent ran; scratch files (/tmp, …), backups and non-path tokens are left out."""
    found = set()
    for cmd in commands:
        paths = [p for pattern in _WRITE_PATTERNS for p in pattern.findall(cmd)]
        paths += [args.split()[-1] for args in _COPY_COMMAND.findall(cmd) if len(args.split()) >= 2]
        if re.search(r"\.write_(?:text|bytes)\(", cmd):
            paths += _PY_PATH.findall(cmd)
        for path in paths:
            path = path.rstrip(";,").removeprefix("/app/").removeprefix("./")
            if path.startswith(_SCRATCH_DIRS) or _BACKUP.search(path):
                continue
            if (_ABSOLUTE if path.startswith("/") else _RELATIVE).fullmatch(path):
                found.add(path)
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

# Under the gateway's strict-schema constrained decoding an unescaped " ends a string field
# early and the answer still parses: every free-text field asks for single quotes.
NO_DOUBLE_QUOTES = " Quote with single quotes, never double quotes."

REVIEW_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "failure_review",
        "strict": True,
        "schema": {
            "type": "object",
            "additionalProperties": False,
            # conclusion comes first: an unescaped " ends a string under the gateway's
            # constrained decoding (aws-iter1-t01's explanation stopped at 'returns status '),
            # so the one sentence the revision needs is written before the long one.
            "required": ["conclusion", "explanation", "attribution", "pitfall_matched", "cited_evidence"],
            "properties": {
                "conclusion": {"type": "string",
                               "description": "One sentence: what is wrong (the agent's mistake, or the task's "
                                              "defect) and where." + NO_DOUBLE_QUOTES},
                "explanation": {"type": "string",
                                "description": "2-4 sentences: what the agent did wrong, citing evidence ids."
                                               + NO_DOUBLE_QUOTES},
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
    """shared/llm/llm.py, imported on first use."""
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
        return {"conclusion": None, "explanation": None, "attribution": UNEXPLAINED, "pitfall_matched": None,
                "cited_evidence": [], "review_error": error}, cost

    known = {e["id"] for e in evidence}
    cited = [i for i in out.get("cited_evidence", []) if i in known]
    attribution = out.get("attribution") if out.get("attribution") in ("model", "task") else UNEXPLAINED
    if not cited:
        attribution = UNEXPLAINED  # an explanation that cites nothing real is not trusted
    return {"conclusion": out.get("conclusion"), "explanation": out.get("explanation"),
            "explanation_cut": is_cut(out.get("explanation")), "attribution": attribution,
            "pitfall_matched": _match_pitfall(out.get("pitfall_matched"), spec),
            "cited_evidence": cited}, cost


def is_cut(text: str | None) -> bool:
    """A model-written field that an unescaped double quote ended early: it stops right where
    the quote was, after a space or an `=`, `:`, `(` or `,` ('returns status ', 'key=')."""
    return bool(text) and bool(re.search(r"(?:\s|[=:(,])$", text))


# --- why was it easy? (review of passing runs) -------------------------------------------
# For a version that came in too easy, one GLM-5.1 call over its fastest passing
# runs: what simple strategy worked, which cues in the task led the agents
# straight to it, and which semantic changes would make that strategy fail. The
# result goes into the revision job, so the next version is hardened against
# what the agents actually did, not against a generic operator.

MAX_PASS_RUNS = 3  # the fastest passing runs are the most informative
PASS_ANALYSIS_STEPS = 4  # agent analyses quoted per run (where it found the cause)
PASS_COMMANDS = 12  # commands quoted per run


def _hardening_techniques() -> dict[str, str]:
    _llm()  # as before the package layout: loads shared.contracts.config (and the repo's .env) first
    from shared.contracts.hardening import HARDENING_TECHNIQUES
    return HARDENING_TECHNIQUES


SOLVER_RUN_BYTES = 10_000  # per passing run, for the world-mutation author
SOLVER_RUN_ANALYSIS_CHARS = 600
# Commands that show the agent a file: cat / head / tail / less / nl / sed -n … FILE.
_READ = re.compile(r"\b(?:cat|head|tail|less|more|nl|sed\s+-n\s+(?:'[^']*'|\"[^\"]*\"|\S+))\s+"
                   r"(?:-\S+\s+)*([\w./-]+\.\w{1,8}|(?:[\w.-]+/)*(?:Makefile|Dockerfile))")


def solver_run_summary(label: str, trial_dir: Path) -> str | None:
    """A passing run, condensed for the world-mutation author (docs: world_mutation.md, "Why it
    was too easy"): which files it read and edited at which step, then each step's analysis and
    commands. At most SOLVER_RUN_BYTES; None without a trajectory."""
    trajectory = load_trajectory(trial_dir)
    if trajectory is None:
        return None
    steps = _agent_steps(trajectory)
    read: dict[str, int] = {}
    edited: dict[str, int] = {}
    body: list[str] = []
    for n, step in enumerate(steps, 1):
        cmds = [(c.get("arguments") or {}).get("keystrokes", "") if isinstance(c.get("arguments"), dict) else ""
                for c in step.get("tool_calls") or []]
        for cmd in cmds:
            for path in _READ.findall(cmd):
                read.setdefault(path.removeprefix("/app/"), n)
            for path in files_modified([cmd]):
                edited.setdefault(path, n)
        lines = [f"### step {n}"]
        if (msg := (step.get("message") or "").strip()):
            lines.append(msg[:SOLVER_RUN_ANALYSIS_CHARS].replace("\n", " "))
        lines += [f"    $ {c.strip()[:CMD_CHARS]}".replace("\n", "\n      ") for c in cmds if c.strip()]
        body.append("\n".join(lines))
    head = [f"# {label}: passed in {len(steps)} agent steps",
            "Read (first step): " + (", ".join(f"{p} ({s})" for p, s in read.items()) or "none detected"),
            "Edited (first step): " + (", ".join(f"{p} ({s})" for p, s in edited.items()) or "none detected"), ""]
    text = "\n".join(head)
    for block in body:
        if len(text) + len(block) + 1 > SOLVER_RUN_BYTES - 60:
            text += f"\n… {len(body) - body.index(block)} more steps not shown\n"
            break
        text += block + "\n"
    return text


def pass_evidence(runs: list[tuple[str, Path]]) -> list[dict]:
    """Numbered evidence lines for passing runs: how fast, what the agent
    concluded early on, the commands that got it there, what it changed."""
    evidence: list[dict] = []

    def add(source: str, text: str) -> None:
        evidence.append({"id": f"E{len(evidence) + 1}", "source": source, "text": text})

    for label, trial_dir in runs:
        trajectory = load_trajectory(trial_dir)
        if trajectory is None:
            add(label, "no agent trajectory was recorded")
            continue
        steps, calls = _agent_steps(trajectory), _tool_calls(trajectory)
        written = files_modified([c["input"] for c in calls])
        add(label, f"passed in {len(steps)} agent steps; files it wrote: {', '.join(written) or 'none detected'}")
        for s in steps[:PASS_ANALYSIS_STEPS]:
            if (s.get("message") or "").strip():
                add(label, f"agent's analysis at step {s.get('step_id')}: {s['message'].strip()[:MESSAGE_CHARS]}")
        for c in calls[:PASS_COMMANDS]:
            if c["tool"] == "mark_task_complete":
                add(label, "agent declared the task complete")
                continue
            out = c["output"].strip()[-OUTPUT_CHARS:]
            add(label, f"$ {c['input'].strip()[:CMD_CHARS]}" + (f"\n→ {out}" if out else ""))
    return evidence


def _pass_review_schema(techniques: list[str]) -> dict:
    return {"type": "json_schema", "json_schema": {"name": "pass_review", "strict": True, "schema": {
        "type": "object", "additionalProperties": False,
        "required": ["why_easy", "simple_strategy", "cues", "fix_applied", "suggestions", "cited_evidence"],
        "properties": {
            "why_easy": {"type": "string", "description": "2-4 sentences: why the agents passed so easily, "
                                                          "citing evidence ids." + NO_DOUBLE_QUOTES},
            "simple_strategy": {"type": "string", "description": "The simple strategy that solved it, in one or "
                                                                 "two sentences." + NO_DOUBLE_QUOTES},
            "cues": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["quote", "where"],
                "properties": {"quote": {"type": "string", "description": "The exact text that pointed the "
                                                                          "agents at the cause or the fix."},
                               "where": {"type": "string", "description": "instruction, a file path, a log, "
                                                                          "an error message, ..."}}}},
            "fix_applied": {"type": "string", "description": "What the passing agents changed." + NO_DOUBLE_QUOTES},
            "suggestions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["technique", "change", "defeats"],
                "properties": {"technique": {"type": "string", "enum": techniques},
                               "change": {"type": "string", "description": "A concrete semantic change to the "
                                                                           "task." + NO_DOUBLE_QUOTES},
                               "defeats": {"type": "string", "description": "Why the observed strategy would no "
                                                                            "longer pass." + NO_DOUBLE_QUOTES}}}},
            "cited_evidence": {"type": "array", "items": {"type": "string"}},
        }}}}


PASS_REVIEW_PROMPT = """An AI coding agent passed this terminal task in {passed} of {runs} runs. The
target is that only 1 to 3 of 5 runs pass, so the task is too easy. Work out
exactly why from the evidence below, then propose how to make it harder. Work
only from the evidence; do not invent facts. Cite evidence ids for every claim.

1. why_easy: why the agents passed so easily (how quickly they found the cause,
   and what made it obvious).
2. simple_strategy: the simple strategy that solved it.
3. cues: the exact text that pointed them at the cause or the fix (a sentence
   of the instruction, a file or function name, a log line, an error message).
4. fix_applied: what the passing agents changed.
5. suggestions: one to three concrete semantic changes to the task that would
   make that exact strategy fail, each using one technique:
{techniques}
   Make the reasoning deeper, not the task bigger. Never suggest vague
   instructions, unstated requirements, more code, larger inputs alone,
   arbitrary restrictions or unreliable tests. Every suggestion must keep the
   task solvable with all requirements stated in the instruction.

## Task instruction (what the agents saw)
{instruction}

## Hidden task spec (the agents never saw this)
{spec}

## Evidence from the passing runs
{evidence}
"""


def review_passes(evidence: list[dict], instruction: str, spec: dict | None, *, passed: int, runs: int,
                  cache_key: str, llm_dir: Path | None, complete: Complete | None = None) -> tuple[dict, float]:
    """One GLM-5.1 review of why a version was too easy. Returns (analysis, cost). Never raises."""
    try:
        return _review_passes(evidence, instruction, spec, passed=passed, runs=runs,
                              cache_key=cache_key, llm_dir=llm_dir, complete=complete)
    except Exception as e:  # prompt building or a malformed reply: never block the decision
        log.warning("%s: pass review failed: %s", cache_key, e)
        return {"why_easy": None, "trusted": False, "review_error": f"{type(e).__name__}: {str(e)[:300]}"}, 0.0


def _review_passes(evidence: list[dict], instruction: str, spec: dict | None, *, passed: int, runs: int,
                   cache_key: str, llm_dir: Path | None, complete: Complete | None) -> tuple[dict, float]:
    techniques = _hardening_techniques()
    spec_text = json.dumps({k: spec.get(k) for k in ("root_cause", "correctness_constraints", "subgoals",
                                                    "expected_agent_pitfalls")}, indent=1) \
        if spec else "(none: this task has no recorded spec)"
    prompt = PASS_REVIEW_PROMPT.format(
        passed=passed, runs=runs, instruction=instruction or "(not available)", spec=spec_text,
        techniques="\n".join(f"   - {name}: {text}" for name, text in techniques.items()),
        evidence="\n".join(f"[{e['id']}] ({e['source']}) {e['text']}" for e in evidence),
    )
    out, cost, error = None, 0.0, None
    for attempt in range(1, REVIEW_ATTEMPTS + 1):
        try:
            complete = complete or _llm().complete
            c = complete([{"role": "user", "content": prompt}], _pass_review_schema(sorted(techniques)),
                         llm_dir=llm_dir, cache_key=cache_key)
            cost += 0.0 if getattr(c, "cached", False) else float(getattr(c, "cost_usd", 0.0) or 0.0)
            out = json.loads(c.content)
            break
        except Exception as e:  # never block the decision on the analysis
            error = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("%s: pass review attempt %d/%d failed: %s", cache_key, attempt, REVIEW_ATTEMPTS, error)
    if out is None:
        return {"why_easy": None, "trusted": False, "review_error": error}, cost

    known = {e["id"] for e in evidence}
    cited = [i for i in out.get("cited_evidence") or [] if i in known]
    suggestions = [s for s in out.get("suggestions") or []
                   if isinstance(s, dict) and s.get("technique") in techniques and s.get("change")][:3]
    cut = [k for k in ("why_easy", "simple_strategy", "fix_applied") if is_cut(out.get(k))]
    cut += [f"suggestions[{i}].change" for i, x in enumerate(suggestions) if is_cut(x.get("change"))]
    return {"why_easy": out.get("why_easy"), "simple_strategy": out.get("simple_strategy"), "cut_fields": cut,
            "cues": [c for c in out.get("cues") or [] if isinstance(c, dict) and c.get("quote")],
            "fix_applied": out.get("fix_applied"), "suggestions": suggestions, "cited_evidence": cited,
            # Like the failure review: an analysis that cites nothing real is kept but not trusted.
            "trusted": bool(cited)}, cost


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


# --- timeouts: a model failure, or not? -------------------------------------------------
# A run that hits the agent time limit gets no automatic verdict (a timeout doesn't count as
# the model failing by itself). One GLM-5.1 call over its last steps, with deterministic
# signals, rules on it: the model's own failure (it wedged its terminal, looped on the same
# commands, abandoned the task, or its own code hung) → VALID_FAIL; still making real progress,
# or slow infrastructure → INVALID, retried. A failed or untrusted review → INVALID.

TIMEOUT_TAIL_STEPS = 8  # the last agent steps quoted
TIMEOUT_REPEAT_WINDOW = 20  # commands checked for repetition
OVERSIZED_COMMAND_BYTES = 4096
MODEL_TIMEOUT_CATEGORIES = ("wedged_terminal", "looping", "abandoned", "own_code_hanging")
NOT_MODEL_TIMEOUT_CATEGORIES = ("progressing", "slow_infrastructure")


def _screen(step: dict) -> str | None:
    """What the terminal showed after a step: the text after its last 'New Terminal Output:'.
    None for a step that showed no terminal (a reply the harness couldn't parse)."""
    text = "\n".join(r.get("content", "") for r in ((step.get("observation") or {}).get("results") or []))
    if "New Terminal Output:" not in text:
        return None
    return text.rsplit("New Terminal Output:", 1)[-1].strip()[-OUTPUT_CHARS:]


def _minutes(a: str | None, b: str | None) -> float | None:
    ta, tb = parse_time(a), parse_time(b)
    return round((tb - ta).total_seconds() / 60, 1) if ta and tb else None


def timeout_signals(trial_dir: Path, result: dict) -> dict:
    """Deterministic facts about how a timed-out run spent its last stretch."""
    trajectory = load_trajectory(trial_dir)
    steps, calls = _agent_steps(trajectory), _tool_calls(trajectory)
    shown = [(i, sc) for i, st in enumerate(steps) if (sc := _screen(st)) is not None]
    start = len(steps) - 1 if steps else 0
    if shown:
        k = len(shown) - 1
        while k > 0 and shown[k][1] == shown[k - 1][1]:
            k -= 1  # the final stretch after which the screen never changed
        start = shown[k][0]
    recent = [c["input"] for c in calls[-TIMEOUT_REPEAT_WINDOW:] if c["input"].strip()]
    sizes = [len(c["input"].encode()) for c in calls]
    biggest = max(range(len(sizes)), key=sizes.__getitem__, default=None)
    step_of_call = [st.get("step_id") for st in steps for _ in st.get("tool_calls") or []]
    timing = result.get("agent_execution") or {}
    return {
        "agent_steps": len(steps),
        "unchanged_screen_steps": len(steps) - start if steps else 0,
        "unchanged_screen_minutes": _minutes(steps[start].get("timestamp"), steps[-1].get("timestamp"))
        if steps else None,
        "unchanged_screen_since_step": steps[start].get("step_id") if steps else None,
        "distinct_recent_commands": f"{len(set(recent))} of {len(recent)}",
        "largest_command_bytes": max(sizes, default=0),
        "largest_command_step": step_of_call[biggest] if biggest is not None and biggest < len(step_of_call)
        else None,
        "oversized_commands": sum(n > OVERSIZED_COMMAND_BYTES for n in sizes),
        "minutes_from_last_step_to_timeout": _minutes(steps[-1].get("timestamp"), timing.get("finished_at"))
        if steps else None,
        "declared_complete": any(c["tool"] == "mark_task_complete" for c in calls),
    }


def timeout_evidence(trial_dir: Path, signals: dict) -> list[dict]:
    """Numbered evidence: the steps where the screen stopped changing, then the last steps."""
    steps = _agent_steps(load_trajectory(trial_dir))
    evidence: list[dict] = []

    def add(source: str, text: str) -> None:
        evidence.append({"id": f"E{len(evidence) + 1}", "source": source, "text": text})

    add("signals", json.dumps(signals))

    def step_line(s: dict) -> str:
        keys = " | ".join(str((c.get("arguments") or {}).get("keystrokes", ""))[:CMD_CHARS]
                          for c in s.get("tool_calls") or [])
        return (f"step {s.get('step_id')} at {s.get('timestamp')}: {(s.get('message') or '').strip()[:400]}"
                + (f"\n$ {keys}" if keys else "") + (f"\n→ {screen[-300:]}" if (screen := _screen(s)) else ""))

    ids = [s.get("step_id") for s in steps]
    since = signals.get("unchanged_screen_since_step")
    first = ids.index(since) if since in ids else len(steps)
    tail = range(max(0, len(steps) - TIMEOUT_TAIL_STEPS), len(steps))
    for i in sorted({*range(max(0, first - 2), min(first + 1, len(steps))), *tail}):
        add("agent", step_line(steps[i]))
    if not steps:
        add("agent", "no agent trajectory was recorded")
    return evidence


TIMEOUT_SCHEMA = {"type": "json_schema", "json_schema": {"name": "timeout_review", "strict": True, "schema": {
    "type": "object", "additionalProperties": False,
    # the enum first (it can't be cut by a stray quote), then the sentence the record shows
    "required": ["category", "reason", "explanation", "cited_evidence"],
    "properties": {
        "category": {"type": "string", "enum": [*MODEL_TIMEOUT_CATEGORIES, *NOT_MODEL_TIMEOUT_CATEGORIES],
                     "description": "wedged_terminal: the agent left its shell unusable (an unclosed heredoc or "
                                    "quote, an interactive program it can't leave) and never recovered. looping: "
                                    "it repeated the same commands or edits without new information. abandoned: "
                                    "it stopped working on the task. own_code_hanging: a command or program the "
                                    "agent wrote hung and it kept waiting. progressing: it was still doing new, "
                                    "useful work when time ran out. slow_infrastructure: model calls, builds or "
                                    "the environment were slow for reasons outside the agent's control."},
        "reason": {"type": "string", "description": "A full sentence, e.g. 'A 26 KB heredoc at step 5 left bash at its "
                                                   "continuation prompt and the agent never got the shell back.'"
                                                   + NO_DOUBLE_QUOTES},
        "explanation": {"type": "string", "description": "2-4 sentences citing evidence ids." + NO_DOUBLE_QUOTES},
        "cited_evidence": {"type": "array", "items": {"type": "string"}},
    }}}}

TIMEOUT_PROMPT = """An AI coding agent ran out of time on a terminal task (the agent time limit).
A timeout does not by itself count as the model failing. Decide from the evidence
only whether the AGENT caused it: it wedged its own terminal, looped, abandoned the
task, or its own code hung. If it was still making real progress, or the slowness
was outside its control, say so. Cite evidence ids for every claim.

The signals line (E1) is computed from the trace: unchanged_screen_steps/minutes is the
final stretch over which the terminal showed the same thing after every step.

## Evidence
{evidence}
"""


def review_timeout(trial_dir: Path, result: dict, *, cache_key: str, llm_dir: Path | None,
                   complete: Complete | None = None) -> tuple[dict, float]:
    """Rule on a timed-out run. Returns ({verdict: model | not_model, category, conclusion,
    explanation, cited_evidence, evidence, signals[, review_error]}, cost). Never raises; a
    failed or uncited review is not_model, so the run is retried, never counted as a fail."""
    signals = timeout_signals(trial_dir, result)
    evidence = timeout_evidence(trial_dir, signals)
    prompt = TIMEOUT_PROMPT.format(evidence="\n".join(f"[{e['id']}] ({e['source']}) {e['text']}" for e in evidence))
    out, cost, error = None, 0.0, None
    for attempt in range(1, REVIEW_ATTEMPTS + 1):
        try:
            complete = complete or _llm().complete
            c = complete([{"role": "user", "content": prompt}], TIMEOUT_SCHEMA, llm_dir=llm_dir, cache_key=cache_key)
            cost += 0.0 if getattr(c, "cached", False) else float(getattr(c, "cost_usd", 0.0) or 0.0)
            out = json.loads(c.content)
            break
        except Exception as e:  # gateway error, bad JSON: retry, then rule not_model
            error = f"{type(e).__name__}: {str(e)[:300]}"
            log.warning("%s: timeout review attempt %d/%d failed: %s", cache_key, attempt, REVIEW_ATTEMPTS, error)
    known = {e["id"] for e in evidence}
    cited = [i for i in (out or {}).get("cited_evidence", []) if i in known]
    category = (out or {}).get("category")
    verdict = "model" if category in MODEL_TIMEOUT_CATEGORIES and cited else "not_model"
    review = {"verdict": verdict, "category": category, "conclusion": (out or {}).get("reason"),
              "explanation": (out or {}).get("explanation"), "cited_evidence": cited,
              "evidence": evidence, "signals": signals}
    if out is None:
        review["review_error"] = error
    elif not cited:
        review["review_error"] = "the review cited no evidence"
    return review, cost


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
