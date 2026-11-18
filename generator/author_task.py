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
                                 n_output_tokens, gateway_cost_usd, adopted},
                   "failure_kind": "infra" | "author"   (FAILED only)},
    }

job_input, by origin:

    {"origin": "initial", "world_sha256", "shape", "dial"}
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
validate gate runs it). FAILED results say whose fault: `infra` when the trial itself
failed (Harbor exception, no trial), `author` when the agent left no valid draft.

Idempotent per job_id: the harness adopts a finished trial under traces/<job_id>/,
bundles are content-addressed, and the draft record is written once.
"""

import hashlib
import json
from pathlib import Path
from string import Template
from typing import Any, Callable

from pydantic import ValidationError

from shared.contracts import config
from shared.taskformat.materialize import write_bundle
from generator.prompting import EASIER_GUIDE, FORMS, HARDENING_TECHNIQUES
from shared.taskformat.schema import BASE_IMAGES, TaskDraft
from generator.worker import _repair_guard, _save_draft_record, format_validation_errors, load_draft_record

PROMPTS = Path(config.PROMPTS_DIR)
AUTHOR_TIMEOUT_SEC = 3600
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
                                         "from the cause, so patching where the symptom shows is not enough.",
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
    "remove_cues": "Keep the world's behavior exactly the same; rewrite the cited cue sentences (in the "
                   "instruction-facing docs, README, logs, comments, names) at the interface level, so they "
                   "no longer point at the cause or the fix.",
}


# --- rendering ----------------------------------------------------------------------


def _system_rules() -> str:
    """system.md's rules and BAD/GOOD examples, with the pinned base images filled in."""
    images = ", ".join(f"`{image}`" for image in sorted(BASE_IMAGES))
    return (PROMPTS / "system.md").read_text().replace("{base_images}", images).strip()


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
    lines += ["", "Invariants:", _bullets(world.get("invariants") or []), "", "Counterexample (hidden from the agent):",
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


def render_task_author_instruction(world: dict, job_input: dict, shape: str, dial: int,
                                   origin: str | None = None) -> str:
    template = Template((PROMPTS / "task_author.md").read_text())
    return template.safe_substitute(
        world_summary=world_summary(world), shape_section=shape_section(shape, dial),
        system_rules=_system_rules(), request=revise_request(job_input, origin),
    ).rstrip() + "\n"


def render_world_mutation_instruction(job_input: dict, parent_world: dict, parent_dir: str = "/input/parent",
                                      output_dir: str = "/logs/artifacts/world") -> str:
    """prompts/world_mutation.md for a `world` job with origin "mutation" (plan §3.6);
    called by author.run_world_job. Unknown operators raise ValueError."""
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
        target_fix=job_input.get("target_fix") or analysis.get("fix_applied") or "(not recorded: use the analysis)",
        operators_section="\n".join(f"- **{op}**: {MUTATION_OPERATORS[op]}" for op in operators),
        why=mutation.get("why") or "(not recorded)",
        history=_bullets([json.dumps(m, sort_keys=True) for m in history[:-1]] if history else []),
        techniques="\n".join(f"- {name}: {text}" for name, text in HARDENING_TECHNIQUES.items()),
    ).rstrip() + "\n"


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
                                      "gateway_cost_usd", "adopted", "exception_type")}


def run_create_job(job_id: str, job_input: dict, work_dir: str | Path | None = None, *,
                   run_trial: Callable[..., dict] | None = None) -> dict:
    """See the module docstring. `run_trial` defaults to the shared authoring harness."""
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

    if trial.get("exception_type") or not trial.get("artifacts_dir"):
        return _failed("infra", trial.get("error") or "the authoring trial left no artifacts", world_sha, trial)
    draft_path = Path(trial["artifacts_dir"]) / "draft.json"
    if not draft_path.is_file():
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
    return {"outcome": "OK", "error": None, "cost_usd": trial.get("cost_usd") or 0.0,
            "result": {"name": draft.name, "bundle_sha256": sha, "bundle_path": str(bundle_path),
                       "draft_path": str(record_path), "world_sha256": world_sha, "llm_calls": [],
                       "authoring": _authoring(trial)}}
