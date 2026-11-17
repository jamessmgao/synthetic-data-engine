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
            len((r.get("trace_metrics") or {}).get("files_modified") or [])
            for r in rollouts
            if r["outcome"] == Outcome.VALID_PASS and r.get("trace_metrics")
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

    Uses the trace parser's `failure` record when there is one, else the
    failing pytest lines the Job Service read from the verifier output
    (`failing_tests`)."""
    lines = []
    for r in sorted(rollouts, key=lambda r: r["slot"]):
        seconds = (r.get("trace_metrics") or {}).get("duration_seconds") or (r.get("result") or {}).get("duration_sec")
        took = f", {seconds:.0f}s" if seconds else ""
        if r["outcome"] == Outcome.VALID_PASS:
            lines.append(f"- run {r['slot']}: passed{took}")
            continue
        failure = r.get("failure") or {}
        tests = failure.get("failing_tests") or r.get("failing_tests") or []
        why = failure.get("explanation") or "; ".join(tests) or "no explanation recorded"
        lines.append(f"- run {r['slot']}: failed{took}: {why}")
    return "\n".join(lines)
