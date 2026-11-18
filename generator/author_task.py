"""
The task author (docs/AGENTIC_AUTHORING_PLAN.md §1, §3.4): from a checked world to a
task bundle, by one agent authoring run (terminus-2 + GLM-5.1 in the authoring image,
generator/authoring_harness.py). generator/author.py re-exports run_create_job; the
Generation Service calls it for `create` and, in agent mode, `revise` jobs.

    run_create_job(job_id, job_input, work_dir=None) -> {
        "outcome": "OK" | "FAILED",
        "error": str | None,
        "cost_usd": float,          # the authoring trial's tokens x price
        "result": {"name", "bundle_sha256", "bundle_path", "draft_path", "world_sha256",
                   "llm_calls": [],  # one agent trial, not chat calls: see `authoring`
                   "authoring": {trial_dir, steps, duration_sec, n_input_tokens,
                                 n_output_tokens, gateway_cost_usd, adopted, exception_type,
                                 instruction_size},
                   "self_check": {by, runs},
                   "leak": {passed, verdict, leaks, direct, strong_hints, blind, blind_leak,
                            calls, cost_usd, adopted, ...} | {error},
                   "failure_kind": "infra" | "author",   (FAILED only)
                   "feedback": str},                      (FAILED by the self-check or the leak review)
    }

job_input, by origin:

    {"origin": "initial", "world_sha256", "shape", "dial", "reused"?, "earlier_instructions"?}
                                  a reused world: the instructions of its earlier tasks
    {"origin": "repair", "bundle_sha256", "feedback", "world_sha256"?}     the gates failed
    {"origin": "easier", "bundle_sha256", "world_sha256", "result", "evidence", "analysis"?}
    {"origin": "repair_ambiguity", "bundle_sha256", "world_sha256", "evidence", "analysis"?}

(A one-shot style `{"origin": "revision", "direction": "easier" | "repair"}` maps to
easier / repair_ambiguity.) A revision without `world_sha256` uses the world its
parent draft was built from.

The author reads the world from <work>/worlds/<sha>/ (mounted at /input/world/) and,
for a revision, the previous draft (/input/previous/draft.json); it writes
/logs/artifacts/draft.json and checks it with `sde-task` before finishing. Here the
draft is validated with TaskDraft (every deterministic pipeline check), a repair may
not drop tests or constraints, and a draft without a wrong fix is rejected (the
validate gate runs it). A valid draft is accepted even if the trial ended with an exception
after writing it (result.authoring.exception_type records it). FAILED results say
whose fault: `infra` when the trial failed without leaving a draft (Harbor exception,
no trial), `author` when a clean trial left no valid draft.

Then the self-check guard (generator/self_check.py:ensure_task_checked: the author's
saved `sde-task check`, or the check run here, free) and the leak review (plan N6):
validator/gates.py:gate_leak, the semantic review against the hidden spec and the
world plus the blind fix prediction (docs/VALIDATION_GATES.md), runs here without a
Docker slot. A failing self-check or a leak is FAILED / author with the feedback (and
the bundle, so after_generation sends it to a repair); the review's cost is added to
the job's, and it is recorded by bundle sha, so validate's leak gate adopts it instead
of paying again. If the review call itself fails, the job still succeeds and validate
runs the review.

Idempotent per job_id: the harness adopts a finished trial under traces/<job_id>/,
bundles are content-addressed, and the draft record and leak review are written once.
"""

import hashlib
import json
import os
import sys
from pathlib import Path
from string import Template
from typing import Any, Callable

from pydantic import ValidationError

from shared.contracts import config
from generator.families import brief_for, domain_line, mechanism_line
from shared.taskformat.materialize import write_bundle
from generator.prompting import EASIER_GUIDE, FORMS, HARDENING_TECHNIQUES
from shared.taskformat.schema import BASE_IMAGES, TaskDraft
from generator.worker import _repair_guard, _save_draft_record, format_validation_errors, load_draft_record

PROMPTS = Path(config.PROMPTS_DIR)
# A safety net for a hung session only (2 h): the prompt suggests ~10 min, it doesn't enforce it.
AUTHOR_TIMEOUT_SEC = float(os.environ.get("SDE_CREATE_TIMEOUT_S", "7200"))
DEFAULT_SHAPE, DEFAULT_DIAL = "fix", 2
REVISE_ORIGINS = ("repair", "easier", "repair_ambiguity")

# What each task shape asks for, and its difficulty dial (plan §3.1). `fix` and
# `implement` reuse the one-shot forms' dials (prompting.FORMS), which the pilots tuned.
_FIX, _IMPL = FORMS["behavior_preserving"], FORMS["many_rules"]
SHAPES: dict[str, dict[str, Any]] = {
    "fix": {
        "what": "The agent's repository is the world with its defect. The agent must make every invariant hold "
                "under every execution the world allows, without breaking anything else that works today.",
        "dial_name": _FIX["dial_name"],
        "dial": _FIX["dial"],
    },
    "implement": {
        "what": "The component at the heart of the world's counterexample is a stub (it raises 'not implemented'). "
                "The agent implements it from the instruction's specification so that every invariant holds; the "
                "rest of the world stays as it is.",
        "dial_name": _IMPL["dial_name"],
        "dial": _IMPL["dial"],
    },
    "optimize": {
        "what": "The world is correct but misses a stated resource bound (time or memory) on a stated workload. "
                "The agent must meet the bound with byte-identical behavior. The counterexample is the workload "
                "that exceeds the bound; the tests enforce the bound with at least a 5x margin between the "
                "reference fix and the starting code, in the 1-CPU container.",
        "dial_name": "how hidden the bottleneck is",
        "dial": {1: "The bottleneck is in one function, visible with a profiler.",
                 2: "The cost comes from an interaction of two components (e.g. a cache defeated by a caller's "
                    "access pattern).",
                 3: "Removing the obvious bottleneck is not enough: a second, dependent cost appears only once the "
                    "first is gone, and exactness constraints rule out the shortcut."},
    },
    "migrate": {
        "what": "The world must move to a new data layout or interface while every invariant keeps holding, "
                "including for data and in-flight work that existed before the change.",
        "dial_name": "how much old state the migration must carry",
        "dial": {1: "Existing data in one format must be converted.",
                 2: "Existing data plus in-flight operations that straddle the change.",
                 3: "Mixed-version state written by an earlier partial migration, plus in-flight operations."},
    },
}

# World mutation operators (plan §3.6, WORLD_CONSTRUCTION.md §8) and what each asks of
# the world author (prompts/world_mutation.md).
MUTATION_OPERATORS = {
    "add_concurrent_execution": "Add a second concurrent actor or interleaving point, so operations that "
                                "were safe in sequence now race on shared state.",
    "inject_partial_failure": "Add a crash, timeout or lost message at a point where it leaves the system "
                              "half-done, so the fix must hold across the failure.",
    "introduce_global_invariant": "Add an invariant over the whole system (a sum, a limit, an ordering) that "
                                  "independently valid local actions can jointly violate.",
    "generate_counterexample": "Construct a concrete input or schedule on which the strategy that passed "
                               "(greedy, first-fit, local) gives a wrong result, and make it part of the world.",
    "tighten_resource_bound": "Tighten a time or memory bound so the strategy that passed no longer fits and "
                              "a different algorithm or architecture is needed.",
    "add_persistent_state": "Make the relevant state survive restarts, so a process-local or in-memory fix "
                            "fails after a restart and recovery/replay must be handled.",
    "extend_cross_component_dependency": "Move the root cause so the symptom shows in a different component "
                                         "from the cause, or make another module depend on the current "
                                         "behavior (a derived value, cache, report or consumer), so patching "
                                         "one place is not enough: the correct fix spans 2+ modules.",
    "add_semantic_edge_case": "Add a realistic case (aliasing, an unusual control flow, a boundary value) on "
                              "which the passing fix silently gives a wrong answer.",
    "add_realistic_distractor": "Add a plausible but wrong lead (a suspicious-looking but correct component, a "
                                "misleading log line) that the obvious diagnosis follows.",
    "compose_failure_modes": "Combine two independently hard conditions, so fixing either alone leaves an "
                             "invariant violated.",
    "expand_state_space": "Add entities, states or inputs that materially change which executions are "
                          "possible (not filler: each must matter to the failure).",
    "introduce_temporal_constraint": "Make correctness depend on the order or timing of operations (expiry, "
                                     "deadlines, happens-before), so an order-blind fix fails.",
    "remove_cues": "Keep the world's behavior the same except where another operator below changes it; "
                   "rewrite the cited cue sentences (in the "
                   "instruction-facing docs, README, logs, comments, names) at the interface level, so they "
                   "no longer point at the cause or the fix, nor describe the failing schedule: no step "
                   "sequence, no trigger input or time step, and no documented crash hook or stage names "
                   "(faults are injected from outside: the tests kill the process).",
}


# --- rendering ----------------------------------------------------------------------


def _system_rules() -> str:
    """system.md's rules and BAD/GOOD examples, with the pinned base images filled in;
    without its one-shot output paragraph ("Return exactly one JSON object that matches
    the provided schema…"): here there is no response schema, the draft is a file."""
    images = ", ".join(f"`{image}`" for image in sorted(BASE_IMAGES))
    text = (PROMPTS / "system.md").read_text().replace("{base_images}", images)
    start, end = text.find("Return exactly one JSON object"), text.find("## How Harbor runs your task")
    if 0 <= start < end:
        text = text[:start] + text[end:]
    # The agent author keeps how Harbor runs the task and the rules the pipeline checks;
    # task_author.md states what makes the task good, and `sde-task check` reports the
    # mistakes. (Shorter context, every step: the round-1 author prompt was 5k tokens.)
    cut = text.find("## What makes the task good")
    return (text[:cut] if cut > 0 else text).strip()


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {i}" for i in items) if items else "- (none recorded)"


def world_summary(world: dict) -> str:
    """The parts of world.json the author must build the task around."""
    spec = world.get("spec") or {}
    ce = world.get("counterexample") or {}
    lines = []
    if spec.get("family"):
        lines.append(f"Family: {spec['family']}; mechanism: {spec.get('mechanism')}; "
                     f"environment: {spec.get('environment')}; verification: {spec.get('verification')}.")
    if spec.get("seed"):
        lines.append(f"The requester's idea: {spec['seed']}")
    invariants = world.get("invariants") or []
    numbered = "\n".join(f"{n}. {i}" for n, i in enumerate(invariants, 1)) or "- (none recorded)"
    lines += ["", "Invariants (each needs a dedicated test marked `# invariant: N`):", numbered, "",
              "Counterexample (hidden from the agent):",
              _bullets(ce.get("sequence") or []), f"Reproduce: {ce.get('reproduce', 'repro/repro.sh')}",
              f"Observed: {ce.get('observed', '(not recorded)')}", "",
              f"Plausible wrong fix (the task's wrong_fix_script): {world.get('plausible_wrong_fix') or '(not recorded)'}"]
    return "\n".join(lines)


def shape_section(shape: str, dial: int) -> str:
    if shape not in SHAPES:
        raise ValueError(f"unknown shape {shape!r}; known: {sorted(SHAPES)}")
    s, dial = SHAPES[shape], min(max(int(dial), 1), 3)
    return (f"Shape: {shape}. {s['what']}\n\n"
            f"Difficulty dial: {s['dial_name']}, level {dial} of 3: {s['dial'][dial]}\n\n"
            "The dial sets how much of the world's difficulty reaches the agent. It never permits hiding a "
            "requirement: everything the tests check is stated in the instruction.")


def revise_request(job_input: dict, origin: str | None = None) -> str:
    """The section that turns a revision into a request (empty for a first version).
    `origin` is the agent-mode origin (_origin), which a one-shot style input maps to."""
    origin = origin or job_input.get("origin")
    head = ("# This is a revision\n\nYour previous draft of this task is at `/input/previous/draft.json`. "
            "Start from it: copy it to `/logs/artifacts/draft.json` and change only what this revision asks for. "
            "Keep every test function and every correctness constraint; you may correct a test that is itself "
            "wrong, never delete or weaken one.\n\n")
    if origin == "repair":
        return head + ("The pipeline built and ran your previous draft, and it failed a gate:\n\n"
                       f"{job_input.get('feedback') or '(no feedback recorded)'}\n\n"
                       "Change only what is needed to fix these problems.")
    if origin == "easier":
        return head + (f"Five agent runs were made on it. {job_input.get('result', '').strip()} That is too hard: "
                       f"the target is 1 to 3 of 5 runs passing.\n\nEvidence from the runs:\n"
                       f"{job_input.get('evidence') or '(none recorded)'}\n\n{EASIER_GUIDE}\n\n"
                       "The world stays the same: change only the task (the instruction's scope, how many "
                       "interacting requirements it carries, how much evidence it points to), never the "
                       "counterexample test.")
    if origin == "repair_ambiguity":
        return head + ("A reviewer of the agents' runs found that the task itself is at fault, not the agents:\n\n"
                       f"{job_input.get('evidence') or '(no evidence recorded)'}\n\n"
                       "Fix exactly that: state in the instruction every requirement the tests check, or fix the "
                       "broken test or environment. Keep the difficulty unchanged.")
    return ""


def earlier_tasks_section(job_input: dict) -> str:
    """For a new task on a reused world: the instructions of the tasks already written on it
    (shared/jobs/worker.py earlier_instructions), which the new one must not repeat. Empty
    otherwise."""
    earlier = job_input.get("earlier_instructions") or []
    if not earlier:
        return ""
    quoted = "\n\n".join(f"Task {n}:\n" + "\n".join(f"> {line}" for line in text.splitlines())
                         for n, text in enumerate(earlier, 1))
    return ("# This world already has a task\n\n"
            f"{'Tasks have' if len(earlier) > 1 else 'A task has'} already been written on this world "
            "(below). Write a different one: another of the world's guarantees or invariants, another "
            "entry point or user-visible symptom; never the same request reworded. The fix may be the "
            "same; the request, the symptom and what the agent must establish must not.\n\n" + quoted)


def render_task_author_instruction(world: dict, job_input: dict, shape: str, dial: int,
                                   origin: str | None = None) -> str:
    template = Template((PROMPTS / "task_author.md").read_text())
    request = revise_request(job_input, origin) or earlier_tasks_section(job_input)
    return template.safe_substitute(
        world_summary=world_summary(world), shape_section=shape_section(shape, dial),
        system_rules=_system_rules(), request=request,
    ).rstrip() + "\n"


def render_world_mutation_instruction(job_input: dict, parent_world: dict, parent_dir: str = "/input/parent",
                                      output_dir: str = "/logs/artifacts/world",
                                      solver_runs_dir: str = "/input/solver_runs") -> str:
    """prompts/world_mutation.md for a `world` job with origin "mutation" (plan §3.6) or
    "variant" (variant_template); called by author.run_world_job. Unknown operators raise ValueError."""
    if job_input.get("origin") == "variant":
        return _render_variant(job_input, parent_world, parent_dir, output_dir)
    mutation = job_input.get("mutation") or {}
    operators = mutation.get("operators") or []
    unknown = [op for op in operators if op not in MUTATION_OPERATORS]
    if unknown or not operators:
        raise ValueError(f"mutation operators {operators!r} must be among {sorted(MUTATION_OPERATORS)}")
    analysis = job_input.get("analysis") or {}
    analysis_lines = [f"{label}: {analysis[key]}" for key, label in (
        ("why_easy", "Why it was easy"), ("simple_strategy", "The strategy that worked"),
        ("fix_applied", "The fix the agents applied")) if analysis.get(key)]
    cues = [f"Cue: \"{c['quote']}\" ({c.get('where', 'task')})" for c in analysis.get("cues") or [] if c.get("quote")]
    history = job_input.get("mutations") or []
    template = Template((PROMPTS / "world_mutation.md").read_text())
    return template.safe_substitute(
        parent_dir=parent_dir, output_dir=output_dir,
        parent_sha=job_input.get("parent_world_sha256") or "",
        parent_summary=world_summary(parent_world),
        analysis=_bullets(analysis_lines + cues),
        solver_runs_line=(f"\nHow the passing agents found it, step by step: {solver_runs_dir}/ (read them; make "
                          "sure your world removes the path they took, including names, comments and tools that "
                          "pointed at the bug)\n") if job_input.get("solver_runs") else "",
        target_fix=job_input.get("target_fix") or analysis.get("fix_applied") or "(not recorded: use the analysis)",
        operators_section="\n".join(f"- **{op}**: {MUTATION_OPERATORS[op]}" for op in operators),
        why=("; ".join(w) if isinstance(w := mutation.get("why"), list) else w) or "(not recorded)",
        history=_bullets([json.dumps(m, sort_keys=True) for m in history[:-1]] if history else []),
        techniques="\n".join(f"- {name}: {text}" for name, text in HARDENING_TECHNIQUES.items()),
        hardness=hardness_section(job_input.get("hardness")),
        # With worlds reused, a mutation is the only new world: the family brief reaches it here.
        family_brief=brief_for((job_input.get("spec") or parent_world.get("spec") or {}).get("family")),
    ).rstrip() + "\n"


MUTATION_WHY_HEADING = "# Why it was too easy"
MUTATION_TO_MAKE_HEADING = "# The mutation to make"


def variant_template() -> str:
    """world_mutation.md for a variant (a reused world's new task: a new scenario on the same
    system, nothing has run on it yet): its opening paragraph is world_variant.md's first
    paragraph, and the block from MUTATION_WHY_HEADING up to MUTATION_TO_MAKE_HEADING is the
    rest of world_variant.md. Everything else (the parent, the operators, the family brief,
    what makes a good mutation, the check contract) is world_mutation.md's."""
    mutation = (PROMPTS / "world_mutation.md").read_text()
    variant = (PROMPTS / "world_variant.md").read_text().strip()
    opening, _, body = variant.partition("\n\n")
    _, _, rest = mutation.partition("\n\n")
    head, found, tail = rest.partition(MUTATION_WHY_HEADING)
    _, found_next, tail = tail.partition(MUTATION_TO_MAKE_HEADING)
    if not (found and found_next):
        raise ValueError(f"world_mutation.md lacks {MUTATION_WHY_HEADING!r} or {MUTATION_TO_MAKE_HEADING!r}")
    return f"{opening}\n\n{head}{body}\n\n{MUTATION_TO_MAKE_HEADING}{tail}"


def _render_variant(job_input: dict, parent_world: dict, parent_dir: str, output_dir: str) -> str:
    operators = (job_input.get("mutation") or {}).get("operators") or []
    unknown = [op for op in operators if op not in MUTATION_OPERATORS]
    if unknown or not operators:
        raise ValueError(f"variant operators {operators!r} must be among {sorted(MUTATION_OPERATORS)}")
    earlier = job_input.get("earlier_instructions") or []
    return Template(variant_template()).safe_substitute(
        parent_dir=parent_dir, output_dir=output_dir,
        parent_sha=job_input.get("parent_world_sha256") or "",
        parent_summary=world_summary(parent_world),
        earlier_tasks=_bullets([" ".join(t.split()) for t in earlier]) if earlier else "(none recorded)",
        mechanism=(job_input.get("spec") or {}).get("mechanism") or "(the family's other mechanism)",
        # world_variant.md (L4) may place these: the reseeded spec's domain and defect pattern.
        domain_line=domain_line(job_input.get("spec") or {}),
        mechanism_line=mechanism_line(job_input.get("spec") or {}),
        operators_section="\n".join(f"- **{op}**: {MUTATION_OPERATORS[op]}" for op in operators),
        why=(job_input.get("mutation") or {}).get("why") or "a new scenario on the same system",
        hardness="(not recorded: nothing has run on this world's variants yet)",
        history="(none: this is the first world of its own line)",
        family_brief=brief_for((job_input.get("spec") or parent_world.get("spec") or {}).get("family")),
        techniques="\n".join(f"- {name}: {text}" for name, text in HARDENING_TECHNIQUES.items()),
    ).rstrip() + "\n"


def hardness_section(hardness: dict | None) -> str:
    """The parent world's hardness checks (world_check's result.hardness: {symptom_ok,
    spans_or_dependent, wrong_fix_ok, failed, warnings, details}), as what this mutation must fix."""
    if not hardness:
        return "(not recorded for the parent world)"
    failed = set(hardness.get("failed") or [])
    details = hardness.get("details") or {}
    found = []
    if "fix_not_local" in failed:
        files = details.get("fix_files")
        found.append(f"its fix is local (world_fix/fix.sh changes {', '.join(files) if files else 'one file'}): "
                     "make the correct fix span 2+ modules, or make another module depend on the current "
                     "behavior, with a repro/dependent_check.sh the one-file patch breaks")
    if failed & {"dependent_holds", "dependent_holds_with_fix", "dependent_breaks_with_wrong_fix"}:
        found.append("its repro/dependent_check.sh did not hold on the world and with the fix, or did not break "
                     "under the wrong fix: the local patch must break the dependent")
    if failed & {"symptom_visible", "symptom_gone_with_fix"}:
        found.append("its repro/symptom_check.sh did not show the symptom on the world, or still did after the "
                     "fix: the visible symptom must appear, and go away with the real fix")
    if "wrong_fix_passes_symptom" in failed:
        found.append("its plausible wrong fix does not fix the visible symptom, so it is not tempting: the new "
                     "wrong fix must make repro/symptom_check.sh pass")
    if "wrong_fix_fails_repro" in failed:
        found.append("its plausible wrong fix survives repro.sh, so it is not wrong: the new one must fail the "
                     "deep schedule")
    if "hardness_files" in failed:
        found.append(f"it lacked {', '.join(details.get('missing') or ['a hardness file'])}: write them")
    for hit in details.get("limit_named") or []:
        found.append(f"{hit.get('file')}: `{hit.get('function')}` names its own limit in its {hit.get('where')} "
                     f"(\"{hit.get('matched')}\"), and the fix changes it: an agent reads that and has the bug. "
                     "Rename it and reword its comments by what it is for")
    known = {"fix_not_local", "dependent_holds", "dependent_holds_with_fix", "dependent_breaks_with_wrong_fix",
             "symptom_visible", "symptom_gone_with_fix", "wrong_fix_passes_symptom", "wrong_fix_fails_repro",
             "hardness_files"}
    if others := sorted(failed - known):
        found.append(f"also failed: {', '.join(others)} (see world.md for each check)")
    if not found:
        return "The parent world passed every hardness check: keep it that way."
    return ("The parent world's checks found the following; the mutation must fix these:\n"
            + "\n".join(f"- {f}" for f in found))


# --- the job ------------------------------------------------------------------------


def _origin(job_input: dict) -> str:
    origin = job_input.get("origin")
    if origin == "revision":  # a one-shot style revise input
        return {"easier": "easier", "repair": "repair_ambiguity"}.get(job_input.get("direction"), "easier")
    if origin not in ("initial", *REVISE_ORIGINS):
        raise ValueError(f"unknown origin {origin!r}")
    return origin


def _world_files(world_dir: Path) -> dict[str, bytes]:
    return {f"world/{p.relative_to(world_dir).as_posix()}": p.read_bytes()
            for p in sorted(world_dir.rglob("*")) if p.is_file()}


def _failed(kind: str, error: str, world_sha: str | None, trial: dict | None) -> dict:
    return {"outcome": "FAILED", "error": error, "cost_usd": (trial or {}).get("cost_usd") or 0.0,
            "result": {"failure_kind": kind, "world_sha256": world_sha, "llm_calls": [],
                       "authoring": _authoring(trial)}}


def _authoring(trial: dict | None) -> dict | None:
    if trial is None:
        return None
    return {k: trial.get(k) for k in ("trial_dir", "steps", "duration_sec", "n_input_tokens", "n_output_tokens",
                                      "gateway_cost_usd", "adopted", "exception_type", "instruction_size")}


def run_create_job(job_id: str, job_input: dict, work_dir: str | Path | None = None, *,
                   run_trial: Callable[..., dict] | None = None,
                   leak_review: Callable[[Path, Path], Any] | None = None,
                   self_check: Callable[[Path, Path, Path], dict] | None = None) -> dict:
    """See the module docstring. `run_trial` defaults to the shared authoring harness,
    `leak_review` to validator/gates.py's gate_leak, `self_check` to
    self_check.ensure_task_checked."""
    work_dir = Path(work_dir or config.WORK_DIR)
    try:
        origin = _origin(job_input)
    except ValueError as e:
        return _failed("author", f"bad job input: {e}", job_input.get("world_sha256"), None)

    previous_record = previous = None
    if origin in REVISE_ORIGINS:
        try:
            previous_record = load_draft_record(job_input["bundle_sha256"], work_dir)
            previous = TaskDraft.model_validate(previous_record["draft"])
        except (KeyError, FileNotFoundError, ValidationError) as e:
            return _failed("infra", f"previous draft unavailable: {type(e).__name__}: {e}",
                           job_input.get("world_sha256"), None)
    meta_before = (previous_record or {}).get("generation_meta") or {}
    world_sha = job_input.get("world_sha256") or meta_before.get("world_sha256")
    shape = job_input.get("shape") or meta_before.get("shape") or DEFAULT_SHAPE
    dial = int(job_input.get("dial") or meta_before.get("dial") or DEFAULT_DIAL)
    if not world_sha:
        return _failed("infra", "no world_sha256 in the job or its parent draft", None, None)
    world_dir = work_dir / "worlds" / world_sha
    try:
        world = json.loads((world_dir / "world.json").read_text())
        instruction = render_task_author_instruction(world, job_input, shape, dial, origin)
    except (FileNotFoundError, json.JSONDecodeError) as e:
        return _failed("infra", f"world {world_sha} unavailable: {type(e).__name__}: {e}", world_sha, None)
    except ValueError as e:
        return _failed("author", f"bad job input: {e}", world_sha, None)

    files = _world_files(world_dir)
    if previous_record is not None:
        files["previous/draft.json"] = json.dumps(previous_record["draft"], indent=1).encode()
    if run_trial is None:
        from generator.authoring_harness import run_authoring_trial as run_trial
    trial = run_trial(job_id, instruction, files, work_dir, timeout_sec=AUTHOR_TIMEOUT_SEC)

    # A valid draft is accepted even if the trial then failed (e.g. a timeout after the
    # author finished): the gates verify it anyway; result.authoring.exception_type records
    # it. Without a draft, a failed trial is infra's fault and a clean one the author's.
    draft_path = Path(trial["artifacts_dir"]) / "draft.json" if trial.get("artifacts_dir") else None
    if draft_path is None or not draft_path.is_file():
        if trial.get("exception_type") or not trial.get("artifacts_dir"):
            return _failed("infra", trial.get("error") or "the authoring trial left no artifacts", world_sha, trial)
        return _failed("author", "the author wrote no /logs/artifacts/draft.json", world_sha, trial)
    try:
        draft = TaskDraft.model_validate_json(draft_path.read_text())
    except ValidationError as e:
        return _failed("author", f"draft.json is not a valid TaskDraft:\n{format_validation_errors(e)}", world_sha, trial)
    if not draft.wrong_fix_script.strip():
        return _failed("author", "draft has no wrong_fix_script (required for agent-authored tasks)", world_sha, trial)
    if origin in REVISE_ORIGINS and (guard := _repair_guard(previous, draft)):
        return _failed("author", f"the revision dropped tests or constraints:\n{guard}", world_sha, trial)

    meta = {"model": config.MODEL, "author_mode": "agent", "origin": origin, "job_id": job_id,
            "world_sha256": world_sha, "shape": shape, "dial": dial,
            "prompt_sha256": hashlib.sha256(instruction.encode()).hexdigest()}
    if origin in REVISE_ORIGINS:
        meta["parent_bundle_sha256"] = job_input["bundle_sha256"]
    sha, bundle_path = write_bundle(draft, work_dir / "bundles", meta)
    record_path = _save_draft_record(sha, work_dir, {"draft": draft.model_dump(), "prompt": instruction,
                                                    "generation_meta": meta, "job_id": job_id})
    built = {"name": draft.name, "bundle_sha256": sha, "bundle_path": str(bundle_path),
             "draft_path": str(record_path), "world_sha256": world_sha, "llm_calls": []}
    cost = trial.get("cost_usd") or 0.0

    # Did the author self-check? Its saved `sde-task check` is used, or the check is run
    # here (deterministic, free): a draft that fails it is repaired now, not after a
    # validate job, and before the leak review spends a model call on it.
    checked = (self_check or _ensure_task_checked)(draft_path, world_dir, Path(trial["artifacts_dir"]))
    # The stamp ties the runs to this exact draft (whose bundle is `sha`): validate adopts
    # them instead of re-running Docker (SDE_ADOPT_AUTHOR_CHECKS).
    built["self_check"] = {"by": checked["by"], "runs": checked["result"].get("runs"),
                           "stamp": checked["result"].get("stamp"), "roles": checked["result"].get("roles")}
    if not checked["ok"]:
        failed = _failed("author", checked["feedback"], world_sha, trial)
        failed["result"].update(built, feedback=checked["feedback"])
        return failed

    # The leak review (plan N6): one glm-5.1 call, no Docker, so it runs here instead of
    # holding a validate slot. It is recorded by bundle sha, so validate adopts it.
    try:
        gate = (leak_review or _leak_review)(Path(bundle_path), work_dir / "traces" / job_id)
    except Exception as e:  # the call failed: validate's leak gate runs it again
        leak = {"error": f"{type(e).__name__}: {str(e)[:500]}"}
    else:
        leak = {"passed": gate.passed, **gate.detail}
        cost += gate.detail.get("cost_usd") or 0.0
        if not gate.passed:
            failed = _failed("author", gate.feedback, world_sha, trial)
            failed["cost_usd"] = round(cost, 6)
            failed["result"].update(built, feedback=gate.feedback, leak=leak)
            return failed
    return {"outcome": "OK", "error": None, "cost_usd": round(cost, 6),
            "result": {**built, "authoring": _authoring(trial), "leak": leak}}


def _ensure_task_checked(draft_path: Path, world_dir: Path, artifacts_dir: Path) -> dict:
    from generator.self_check import ensure_task_checked

    return ensure_task_checked(draft_path, world_dir, artifacts_dir)


def _leak_review(bundle_dir: Path, trace_dir: Path):
    """validator/gates.py's leak gate (validator/ sits next to generator/ here and in the image)."""
    validator = str(Path(__file__).resolve().parent.parent / "validator")
    if validator not in sys.path:
        sys.path.insert(0, validator)
    from validator.gates import gate_leak

    return gate_leak(bundle_dir, trace_dir)
