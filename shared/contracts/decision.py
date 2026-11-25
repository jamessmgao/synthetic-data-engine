"""How a task's decision reads: one mark per agent run, the one-line decision, and what started a
version. The API shows them and the Trace Parser writes them into verdict.md."""

from typing import Any

from shared.contracts.band import eval_config
from shared.contracts.models import GENERATION_TYPES, SCORED_OUTCOMES, JobType, Outcome, TaskStatus

MARKS = {Outcome.VALID_PASS: "✓", Outcome.VALID_FAIL: "✗"}


def pass_pattern(jobs: list[dict[str, Any]], version: int, n_runs: int) -> str:
    """One mark per rollout slot: ✓ pass, ✗ fail, · not scored yet."""
    scored = {
        j["slot"]: j["outcome"]
        for j in jobs
        if j["type"] == JobType.ROLLOUT and j["version"] == version and j["outcome"] in SCORED_OUTCOMES
    }
    return "".join(MARKS.get(scored.get(s), "·") for s in range(1, n_runs + 1))


def decision_text(task: dict[str, Any], jobs: list[dict[str, Any]]) -> str:
    """One line a reviewer can check against the rollout table."""
    cfg = eval_config()
    low, high = cfg["band"]
    if task["status"] == TaskStatus.ACCEPTED:
        v = task["accepted_version"]
        runs = [j for j in jobs if j["type"] == JobType.ROLLOUT and j["version"] == v]
        passes = sum(j["outcome"] == Outcome.VALID_PASS for j in runs)
        if any(j["outcome"] == Outcome.SKIPPED for j in runs):  # early stopping: decided on fewer runs
            scored = sum(j["outcome"] in (Outcome.VALID_PASS, Outcome.VALID_FAIL) for j in runs)
            return (f"ACCEPTED: v{v} passed {passes} of {scored} scored agent runs, early-stopped "
                    f"(band {low}–{high} of {cfg['n_runs']} was certain)")
        return f"ACCEPTED: v{v} passed {passes}/{cfg['n_runs']} valid rollouts (band {low}–{high})"
    if task["status"] == TaskStatus.REJECTED:
        return f"REJECTED ({task['reason']}) at v{task['current_version']}"
    if task["status"] == TaskStatus.CANCELLED:
        by = task.get("cancelled_by") or "user"
        note = f": {task['cancel_note']}" if task.get("cancel_note") else ""
        return f"CANCELLED by {by} at {task.get('cancelled_at') or task['updated_at']} (v{task['current_version']}){note}"
    return f"PENDING: v{task['current_version']} is {task['stage']}"


def version_input(vjobs: list[dict[str, Any]]) -> dict[str, Any]:
    """What started a version: its first world / create / revise job that isn't a repair.
    In agent mode a mutated version starts with a `world` job (origin "mutation", its
    operators and history); the create after the world check just says "initial"."""
    starters = [j for j in vjobs if j["type"] in (JobType.WORLD, *GENERATION_TYPES)
                and (j.get("input") or {}).get("origin") != "repair"]
    return (starters[0].get("input") or {}) if starters else {}
