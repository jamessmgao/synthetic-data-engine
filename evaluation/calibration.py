"""
Difficulty decisions for one evaluated version (docs/PRD.md §6.6): the pinned
band, which revision operator to try next, and the evidence handed to GLM-5.1.

Operator names are task-builder's `prompting.OPERATORS`.
"""

import hashlib
import json
from statistics import median
from typing import Any

from shared.contracts import config
from shared.contracts.models import Outcome


def eval_config() -> dict[str, Any]:
    """The pinned evaluation config every scored rollout of a version must share."""
    return {
        "agent": config.EVAL_AGENT,
        "model": config.EVAL_MODEL,
        "agent_kwargs": {"reasoning_effort": config.EVAL_REASONING_EFFORT},
        "n_runs": config.N_RUNS,
        "band": [config.BAND_LOW, config.BAND_HIGH],
    }


def config_hash() -> str:
    return hashlib.sha256(json.dumps(eval_config(), sort_keys=True).encode()).hexdigest()[:16]


def in_band(pass_count: int) -> bool:
    return config.BAND_LOW <= pass_count <= config.BAND_HIGH


def too_easy(pass_count: int) -> bool:
    return pass_count > config.BAND_HIGH


def too_hard(pass_count: int) -> bool:
    return pass_count < config.BAND_LOW

# Tried in order; an operator already used on this task is skipped.
HARDER = ["increase_diagnosis_depth", "increase_constraint_interaction", "increase_cross_component_dependency"]
EASIER = ["localize_diagnosis", "reduce_unrelated_constraints"]


def choose_operator(pass_count: int, rollouts: list[dict], used: list[str]) -> str:
    """Fixed rules over the evidence: too easy → harder, 0/5 → easier."""
    if pass_count == 0:
        order = list(EASIER)
    else:
        order = list(HARDER)
        # Passing runs that each touched one file at most: the fix is a local
        # patch, so make it span two modules first.
        touched = [
            len(files)
            for r in rollouts
            if r["outcome"] == Outcome.VALID_PASS
            and (files := (r.get("trace_metrics") or {}).get("files_modified")) is not None
        ]
        if touched and median(touched) <= 1:
            order.remove("increase_cross_component_dependency")
            order.insert(0, "increase_cross_component_dependency")
    fresh = [op for op in order if op not in used]
    return (fresh or order)[0]


def failing_tests(verifier_stdout: str) -> list[str]:
    """`FAILED path::test - message` lines from pytest's -rA summary."""
    return [line.removeprefix("FAILED ").strip() for line in verifier_stdout.splitlines() if line.startswith("FAILED ")]


def evidence(rollouts: list[dict]) -> str:
    """One line per scored run, for the revision prompt.

    Uses the trace parser's `failure` record (its explanation, else its failing
    tests), else any `failing_tests` the caller attached."""
    lines = []
    for r in sorted(rollouts, key=lambda r: r["slot"]):
        metrics = r.get("trace_metrics") or {}
        seconds = metrics.get("duration_seconds") or (r.get("result") or {}).get("duration_sec")
        took = f", {seconds:.0f}s" if seconds else ""
        if r["outcome"] == Outcome.VALID_PASS:
            # How easily it passed is part of the evidence: steps and what it edited.
            steps = f", {metrics['llm_calls']} agent steps" if metrics.get("llm_calls") else ""
            files = metrics.get("files_modified")
            edited = f", edited {', '.join(files[:5])}" if files else ""
            lines.append(f"- run {r['slot']}: passed{took}{steps}{edited}")
            continue
        failure = r.get("failure") or {}
        tests = failure.get("failing_tests") or r.get("failing_tests") or []
        why = failure.get("explanation") or "; ".join(tests) or "no explanation recorded"
        lines.append(f"- run {r['slot']}: failed{took}: {why}")
    return "\n".join(lines)


# World mutation operators (docs/AGENTIC_AUTHORING_PLAN.md §3.5, WORLD_CONSTRUCTION.md §8).
MUTATION_OPERATORS = [
    "add_concurrent_execution", "inject_partial_failure", "introduce_global_invariant",
    "generate_counterexample", "tighten_resource_bound", "add_persistent_state",
    "extend_cross_component_dependency", "add_semantic_edge_case", "add_realistic_distractor",
    "compose_failure_modes", "expand_state_space", "introduce_temporal_constraint",
]
MAX_MUTATIONS = 2

# What the passing agents' fix or strategy looked like → the mutation that defeats it.
# Matched against the pass review's fix_applied/simple_strategy and the passing runs'
# commands, in this order.
_FIX_RULES: list[tuple[tuple[str, ...], str, str]] = [
    (("process-local", "in-memory", "in memory", "set()", "seen set", "module-level", "global variable",
      "class attribute", "dict of processed"),
     "add_persistent_state", "the passing fix kept state in one process; a restart must defeat it"),
    (("greedy", "first-fit", "first fit", "sort by", "sorted(", "heuristic", "locally"),
     "generate_counterexample", "a locally reasonable strategy passed; add an input where it is globally wrong"),
    (("lock", "mutex", "synchronized", "serializ", "single thread", "sequential"),
     "inject_partial_failure",
     "serializing the work passed; a crash or timeout inside the critical section must break it"),
    (("retry", "sleep", "timeout", "backoff"),
     "introduce_temporal_constraint", "the fix leaned on timing; the order of operations must matter"),
    (("special-case", "special case", "hardcod", "edge case"),
     "add_semantic_edge_case", "a special case passed; the rule must hold across cases it doesn't name"),
]


def choose_mutations(pass_count: int, rollouts: list[dict], analysis: dict | None,
                     history: list[dict]) -> dict[str, Any]:
    """Which world mutations the next version applies, from the evidence (fixed rules, no model).

    Returns {"operators": [...], "why": [...], "target_fix": str | None}. Too easy: up to
    MAX_MUTATIONS operators aimed at the strategy the passing agents used, and target_fix is
    that strategy. Otherwise no operators (the revision eases or repairs the world) and
    target_fix is the fix that failed. Operators already in `history` come last, so
    successive versions try something new."""
    if not too_easy(pass_count):
        failed = [(r.get("failure") or {}).get("explanation") for r in sorted(rollouts, key=lambda r: r["slot"])
                  if r["outcome"] != Outcome.VALID_PASS]
        return {"operators": [], "why": [], "target_fix": next((f for f in failed if f), None)}

    analysis = analysis if (analysis or {}).get("trusted") else {}
    passing = [r for r in rollouts if r["outcome"] == Outcome.VALID_PASS]
    commands = " ".join(c.get("input", "") for r in passing
                        for c in (r.get("trace_metrics") or {}).get("tool_calls") or [])
    text = " ".join(filter(None, [analysis.get("fix_applied"), analysis.get("simple_strategy"),
                                  commands])).lower()

    hits = [(op, why) for needles, op, why in _FIX_RULES if any(n in text for n in needles)]
    touched = [len(f) for r in passing if (f := (r.get("trace_metrics") or {}).get("files_modified")) is not None]
    if touched and median(touched) <= 1:
        hits.append(("extend_cross_component_dependency",
                     "the passing runs edited one file; the fix must span modules"))
    if analysis.get("cues"):
        hits.append(("add_realistic_distractor",
                     "the instruction's wording pointed at the fix; add a plausible wrong lead"))
    steps = [s for r in passing if (s := (r.get("trace_metrics") or {}).get("llm_calls"))]
    if steps and median(steps) <= 8:
        hits.append(("compose_failure_modes", f"passing runs took a median of {median(steps):.0f} steps; "
                                              "a second independent failure must be found too"))
    hits += [("introduce_global_invariant", "make independently valid actions interact"),
             ("expand_state_space", "add entities or inputs that change the behavior")]

    used = {op for m in history for op in m.get("operators") or []}
    chosen: list[tuple[str, str]] = []
    for fresh in (True, False):
        for op, why in hits:
            if len(chosen) < MAX_MUTATIONS and (op not in used) == fresh and op not in dict(chosen):
                chosen.append((op, why))
    return {"operators": [op for op, _ in chosen], "why": [why for _, why in chosen],
            "target_fix": analysis.get("fix_applied") or analysis.get("simple_strategy")}
