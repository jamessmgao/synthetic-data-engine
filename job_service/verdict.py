"""verdict.md: why a task got its verdict, readable without the code (PRD §6.8)."""

import json
from typing import Any

from shared.contracts.models import SCORED_OUTCOMES, JobType, Outcome

MARKS = {Outcome.VALID_PASS: "✓", Outcome.VALID_FAIL: "✗"}


def pass_pattern(jobs: list[dict[str, Any]], version: int, n_runs: int) -> str:
    """One mark per rollout slot: ✓ pass, ✗ fail, · not scored yet."""
    scored = {
        j["slot"]: j["outcome"]
        for j in jobs
        if j["type"] == JobType.ROLLOUT and j["version"] == version and j["outcome"] in SCORED_OUTCOMES
    }
    return "".join(MARKS.get(scored.get(s), "·") for s in range(1, n_runs + 1))


def render(task: dict[str, Any], jobs: list[dict[str, Any]], decision: str, n_runs: int) -> str:
    lines = [f"# Verdict: {task['id']}", "", f"**{decision}**", ""]

    lines += ["## Spec", "", "```json", json.dumps(task.get("slot") or {}, indent=2), "```", ""]

    lines += ["## Versions", "", "| v | origin | operator | bundle | gates | runs |", "|---|---|---|---|---|---|"]
    for v in range(1, task["current_version"] + 1):
        vjobs = [j for j in jobs if j["version"] == v]
        gen = next((j for j in vjobs if j["type"] == JobType.GENERATE), None)
        inp = (gen or {}).get("input") or {}
        built = [j for j in vjobs if j["type"] == JobType.GENERATE and j["outcome"] == Outcome.OK]
        validates = [j for j in vjobs if j["type"] == JobType.VALIDATE]
        gates = validates[-1]["outcome"] if validates else "-"
        sha = (built[-1]["bundle_sha256"] or "")[:12] if built else "-"
        lines.append(
            f"| {v} | {inp.get('origin', '-')} | {inp.get('operator') or '-'} | `{sha}` | {gates} "
            f"| {pass_pattern(jobs, v, n_runs)} |"
        )
    lines.append("")

    rollouts = sorted(
        (j for j in jobs if j["type"] == JobType.ROLLOUT), key=lambda j: (j["version"], j["slot"], j["retry"])
    )
    lines += ["## Rollouts", "", "| job | outcome | reward | error | cost | trace |", "|---|---|---|---|---|---|"]
    for j in rollouts:
        cost = f"${j['cost_usd']:.4f}" if j["cost_usd"] is not None else "-"
        lines.append(
            f"| {j['id']} | {j['outcome'] or j['status']} | {j['reward'] if j['reward'] is not None else '-'} "
            f"| {j['error'] or ''} | {cost} | {j['trace_uri'] or '-'} |"
        )
    lines.append("")

    failures = [j for j in rollouts if j["outcome"] == Outcome.VALID_FAIL]
    if failures:
        lines += ["## Why runs failed", ""]
        for j in failures:
            f = j.get("failure") or {}
            lines.append(f"### {j['id']}")
            lines.append(f"- attribution: {f.get('attribution', 'unexplained')}")
            if f.get("failing_tests"):
                lines.append(f"- failing tests: {', '.join(f['failing_tests'])}")
            lines.append(f"- explanation: {f.get('explanation') or 'unexplained'}")
            for quote in f.get("evidence") or []:
                lines.append(f"  > {quote}")
            lines.append("")

    costs = [j["cost_usd"] for j in jobs if j["cost_usd"] is not None]
    lines += ["## Cost", "", f"${sum(costs):.4f} recorded across {len(costs)} jobs." if costs else "No cost recorded."]
    return "\n".join(lines) + "\n"
