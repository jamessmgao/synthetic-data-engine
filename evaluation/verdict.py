"""verdict.md: why a task got its verdict, readable without the code (PRD §6.8), and
publishing an accepted task to output/accepted/ (the Trace Parser does this)."""

import json
from pathlib import Path
from typing import Any

from shared.contracts.band import eval_config
from shared.contracts.decision import MARKS, decision_text, pass_pattern, version_input  # noqa: F401
from shared.contracts.models import GENERATION_TYPES, JobType, Outcome

def _gates(validate: dict[str, Any]) -> str:
    """The validate outcome, plus the model-judged gates that ran (wrong fix, leak)."""
    gates = (validate.get("result") or {}).get("gates") or {}
    marks = [f"{name} {'skipped' if g.get('skipped') else '✓' if g.get('passed') else '✗'}"
             for name in ("wrong_fix", "leak") if isinstance(g := gates.get(name), dict)]
    return f"{validate['outcome']}" + (f" ({', '.join(marks)})" if marks else "")


def _world(vjobs: list[dict[str, Any]]) -> str:
    """Agent mode: the world this version's task was written from, and its world_check outcome."""
    worlds = [w for j in vjobs if (w := (j.get("result") or {}).get("world_sha256"))]
    if not worlds:
        return "-"
    checks = [j for j in vjobs if j["type"] == "world_check"]
    return f"`{worlds[-1][:12]}`" + (f" ({checks[-1]['outcome'] or checks[-1]['status']})" if checks else "")


def render(task: dict[str, Any], jobs: list[dict[str, Any]], decision: str, n_runs: int) -> str:
    lines = [f"# Verdict: {task['id']}", "", f"**{decision}**", ""]

    lines += ["## Spec", "", "```json", json.dumps(task.get("slot") or {}, indent=2), "```", ""]

    lines += ["## Versions", "", "| v | origin | operator | mutations | world | bundle | gates | runs |",
              "|---|---|---|---|---|---|---|---|"]
    mutations: list[dict[str, Any]] = []
    for v in range(1, task["current_version"] + 1):
        vjobs = [j for j in jobs if j["version"] == v]
        drafts = [j for j in vjobs if j["type"] in GENERATION_TYPES]
        inp = version_input(vjobs)
        mutations = inp.get("mutations") or mutations  # each revision carries the whole history
        built = [j for j in drafts if j["outcome"] == Outcome.OK]
        validates = [j for j in vjobs if j["type"] == JobType.VALIDATE]
        gates = _gates(validates[-1]) if validates else "-"
        sha = (built[-1]["bundle_sha256"] or "")[:12] if built else "-"
        operators = inp.get("mutation_operators") or (inp.get("mutation") or {}).get("operators") or []
        lines.append(
            f"| {v} | {inp.get('origin', '-')} | {inp.get('operator') or '-'} "
            f"| {', '.join(operators) or '-'} | {_world(vjobs)} | `{sha}` | {gates} "
            f"| {pass_pattern(jobs, v, n_runs)} |"
        )
    lines.append("")

    if mutations:
        lines += ["## World mutations", ""]
        for m in mutations:
            ops = ", ".join(m.get("operators") or []) or "none"
            lines.append(f"- v{m.get('from_version')} → v{m.get('version')} ({m.get('pass_count')}/{n_runs} passed, "
                         f"{m.get('direction')}): {ops}")
            for why in m.get("why") or []:
                lines.append(f"  - {why}")
            if m.get("target_fix"):
                lines.append(f"  - aimed at: {m['target_fix']}")
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
                text = quote.get("text") if isinstance(quote, dict) else quote
                lines.append(f"  > {text}")
            lines.append("")

    costs = [j["cost_usd"] for j in jobs if j["cost_usd"] is not None]
    lines += ["## Cost", "", f"${sum(costs):.4f} recorded across {len(costs)} jobs." if costs else "No cost recorded."]
    return "\n".join(lines) + "\n"


# --- publishing ---------------------------------------------------------------------


def _destinations(accepted_dir: Path, task: dict[str, Any]) -> list[Path]:
    """Where an accepted task goes: output/accepted/<name>/, or
    <name>-<task id>/ if another task already took the name."""
    name = task.get("name") or task["id"]
    return [accepted_dir / name, accepted_dir / f"{name}-{task['id']}"]


def _owned_by(dest: Path, task: dict[str, Any]) -> bool:
    verdict_md = dest / "verdict.md"
    return verdict_md.exists() and verdict_md.read_text().startswith(f"# Verdict: {task['id']}\n")


def published_dir(accepted_dir: Path, task: dict[str, Any]) -> Path | None:
    return next((d for d in _destinations(accepted_dir, task) if _owned_by(d, task)), None)


def publish(artifacts, accepted_dir: Path, task: dict[str, Any], jobs: list[dict[str, Any]]) -> Path:
    """Copy the accepted bundle and its verdict.md to output/accepted/.

    Runs after the ACCEPTED commit and is idempotent; if the process dies
    before it finishes, the next pass publishes again.
    """
    if (done := published_dir(accepted_dir, task)) is not None:
        return done
    mine, fallback = _destinations(accepted_dir, task)
    # Fall back only if *another* task holds the name. A directory appears
    # whole (copied to a temp dir, then renamed), so if a second copy of this
    # service published it a moment ago, it already carries our verdict and
    # publishing to `mine` again is a no-op.
    dest = fallback if mine.exists() and not _owned_by(mine, task) else mine
    text = render(task, jobs, decision_text(task, jobs), eval_config()["n_runs"])
    artifacts.publish(task["bundle_sha256"], dest, text)
    return dest
