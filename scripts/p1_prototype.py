"""
P1 (docs/AUTHORING.md §2): the agent-mode chain for a few tasks, run
locally and in order, without the queue, to see whether world-first authoring
produces tasks in the band and what it costs.

    python scripts/p1_prototype.py [--run p1-a] [--tasks 3] [--family F ...] [--mutation]
    python scripts/p1_prototype.py --execute --budget-usd 15 [...same options...]

Per task:

    world (+ up to 2 world repairs) → world_check → create (+ up to 2 repairs, after a
    leak or a failed gate) → validate → 5 rollouts (terminus-2 + glm-5.3-flash)
    [--mutation, when 4-5 passed: one round of world mutation → world_check → create →
     validate → 5 rollouts, operators chosen by evaluation.calibration.choose_mutations]

**Dry run by default**: prints the plan (specs, steps, estimated cost and time) and a
readiness check, and calls nothing. `--execute` requires `--budget-usd`: before each
paid step it stops if the run's actual spend so far plus that step's estimate would
exceed the budget.

**Resumable for free**: every finished step is recorded in
output/p1/<run>/ledger.json and skipped on a rerun; underneath, each job function also
adopts its own finished trial under traces/<job_id>. A crash or a budget stop is
resumed by running the same command again (with a larger budget if it stopped there).

Writes output/p1/<run>/summary.md: per task, every step's outcome, cost, duration and
whether it was adopted, the pass pattern, and the totals.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
for p in (ROOT, ROOT / "generator", ROOT / "validator"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from shared.taxonomy.taxonomy import sample_spec_v2  # noqa: E402

P1_FAMILIES = ["distributed_concurrency", "database_consistency", "algorithmic_constraints"]
N_RUNS, MAX_REPAIRS, MAX_ROLLOUT_RETRIES = 5, 2, 2

# Estimated USD and minutes per step (agent trials dominate; refine after P1).
ESTIMATE = {
    "world": (3.0, 30), "world_repair": (3.0, 30), "world_mutation": (3.0, 30), "world_check": (0.0, 3),
    "create": (2.5, 30), "repair": (2.5, 30), "validate": (0.05, 5), "rollout": (0.03, 8),
}


class BudgetStop(Exception):
    pass


# --- the job functions (real by default, fakes in tests) -----------------------------


@dataclass
class Chain:
    world: Callable[[str, dict, Path], dict]
    world_check: Callable[[Path, Path], dict]
    create: Callable[[str, dict, Path], dict]
    validate: Callable[[Path, Path], dict]
    rollout: Callable[[Path, str, Path], dict]
    choose_mutations: Callable[..., dict]


def real_chain() -> Chain:
    from generator.author import run_world_job  # generator/author.py (TB2)
    from generator.author_task import run_create_job
    from evaluation.calibration import choose_mutations
    from validator.gates import run_validate_job
    from shared.taskformat.world_check import run_world_check_job

    return Chain(world=run_world_job, world_check=run_world_check_job, create=run_create_job,
                 validate=run_validate_job, rollout=harbor_rollout, choose_mutations=choose_mutations)


def harbor_rollout(bundle_dir: Path, job_id: str, traces_dir: Path) -> dict:
    """One terminus-2 + glm-5.3-flash trial, classified like the executor does."""
    from evaluation import harbor
    from evaluation.classify import classify
    from evaluation.trace_parser import trace_metrics

    agent = harbor.pinned_agent()
    try:
        run = harbor.run_trial(bundle_dir, job_id, agent, threading.Event(), traces_dir=traces_dir)
    except Exception as e:  # Harbor failed to produce a trial: not a measurement
        return {"outcome": "INVALID", "reward": None, "cost_usd": 0.0, "error": f"{type(e).__name__}: {e}"}
    m = classify(run.result, agent.as_expected())
    return {"outcome": m.outcome, "reward": m.reward, "cost_usd": m.cost_usd or 0.0, "error": m.error,
            "duration_sec": m.duration_sec, "adopted": run.adopted,
            "trace_metrics": trace_metrics(run.trial_dir, run.result)}


# --- the ledger ---------------------------------------------------------------------------


class Ledger:
    """Every finished step of a run, keyed by step id; written atomically after each."""

    def __init__(self, path: Path):
        self.path = path
        self.steps: dict[str, dict] = json.loads(path.read_text()) if path.exists() else {}

    def spent(self) -> float:
        return round(sum(s.get("cost_usd") or 0.0 for s in self.steps.values()), 6)

    def record(self, step_id: str, entry: dict) -> None:
        self.steps[step_id] = entry
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(f".tmp{os.getpid()}")
        tmp.write_text(json.dumps(self.steps, indent=1, default=str) + "\n")
        os.replace(tmp, self.path)


@dataclass
class Run:
    run_id: str
    work: Path
    out: Path
    chain: Chain | None
    budget: float
    ledger: Ledger = field(init=False)

    def __post_init__(self):
        self.ledger = Ledger(self.out / "ledger.json")

    def step(self, step_id: str, kind: str, task: str, fn: Callable[[], dict]) -> dict:
        """Run one step once: adopt a recorded result, else check the budget, call, record."""
        if step_id in self.ledger.steps:
            return self.ledger.steps[step_id]["out"]
        estimate = ESTIMATE[kind][0]
        if self.ledger.spent() + estimate > self.budget:
            raise BudgetStop(f"{step_id} ({kind}, est. ${estimate:.2f}) would take the spend from "
                             f"${self.ledger.spent():.2f} past the ${self.budget:.2f} budget")
        t0 = time.monotonic()
        out = fn()
        self.ledger.record(step_id, {"kind": kind, "task": task, "outcome": out.get("outcome"),
                                     "cost_usd": out.get("cost_usd") or 0.0,
                                     "duration_sec": round(time.monotonic() - t0, 1),
                                     "adopted": bool(out.get("adopted") or (out.get("result") or {}).get(
                                         "authoring", {}).get("adopted")),
                                     "error": out.get("error"), "out": out})
        return out


# --- one task -----------------------------------------------------------------------------


def build_world(run: Run, t: str, v: int, first_input: dict, first_kind: str) -> str | None:
    """world (or its mutation) → world_check, repairing the world up to MAX_REPAIRS times."""
    job_input, kind = first_input, first_kind
    for k in range(MAX_REPAIRS + 1):
        wid = f"{t}-v{v}-world" if k == 0 else f"{t}-v{v}-world-repair{k}"
        out = run.step(wid, kind, t, lambda: run.chain.world(wid, job_input, run.work))
        sha = (out.get("result") or {}).get("world_sha256")
        if out.get("outcome") != "OK" or not sha:
            return None
        cid = f"{t}-v{v}-world-check-r{k}"
        check = run.step(cid, "world_check", t, lambda: run.chain.world_check(
            run.work / "worlds" / sha, run.work / "traces" / cid))
        if check.get("outcome") == "OK":
            return sha
        job_input = {"origin": "repair", "world_sha256": sha, "spec": first_input.get("spec"),
                     "feedback": check.get("feedback") or "the world check failed"}
        kind = "world_repair"
    return None


def build_task(run: Run, t: str, v: int, world_sha: str, spec: dict) -> str | None:
    """create → validate, repairing after a leak (create) or a failed gate (validate)."""
    job_input: dict = {"origin": "initial", "world_sha256": world_sha, "shape": spec.get("shape"),
                       "dial": spec.get("dial")}
    kind = "create"
    for k in range(MAX_REPAIRS + 1):
        gid = f"{t}-v{v}-create" if k == 0 else f"{t}-v{v}-repair{k}"
        out = run.step(gid, kind, t, lambda: run.chain.create(gid, job_input, run.work))
        res = out.get("result") or {}
        sha = res.get("bundle_sha256")
        if out.get("outcome") != "OK":
            if res.get("failure_kind") == "author" and sha:  # e.g. the leak review: repair it
                job_input = {"origin": "repair", "bundle_sha256": sha, "world_sha256": world_sha,
                             "feedback": res.get("feedback") or out.get("error")}
                kind = "repair"
                continue
            return None
        vid = f"{t}-v{v}-validate-r{k}"
        val = run.step(vid, "validate", t, lambda: run.chain.validate(
            run.work / "bundles" / sha, run.work / "traces" / vid))
        if val.get("outcome") == "OK":
            return sha
        job_input = {"origin": "repair", "bundle_sha256": sha, "world_sha256": world_sha,
                     "feedback": val.get("feedback") or "a gate failed"}
        kind = "repair"
    return None


def evaluate(run: Run, t: str, v: int, bundle_sha: str) -> list[dict]:
    """5 valid rollouts; an INVALID one is retried in its slot (never scored)."""
    rollouts = []
    for slot in range(1, N_RUNS + 1):
        for retry in range(MAX_ROLLOUT_RETRIES + 1):
            rid = f"{t}-v{v}-rollout{slot}-r{retry}"
            out = run.step(rid, "rollout", t, lambda: run.chain.rollout(
                run.work / "bundles" / bundle_sha, rid, run.work / "traces"))
            if out.get("outcome") in ("VALID_PASS", "VALID_FAIL"):
                rollouts.append({**out, "slot": slot})
                break
    return rollouts


def run_task(run: Run, i: int, spec: dict, mutation: bool, tasks: list[dict]) -> None:
    """Run one task, appending its record to `tasks` first, so a budget stop midway
    still leaves its partial record for the summary."""
    t = f"{run.run_id}-t{i:02d}"
    rec: dict[str, Any] = {"task": t, "spec": spec, "versions": []}
    tasks.append(rec)
    world_input, world_kind = {"origin": "initial", "spec": spec}, "world"
    history: list[dict] = []
    for v in (1, 2) if mutation else (1,):
        ver: dict[str, Any] = {"v": v}
        rec["versions"].append(ver)
        world = build_world(run, t, v, world_input, world_kind)
        if not world:
            ver["verdict"] = "invalid_world"
            break
        ver["world_sha256"] = world
        bundle = build_task(run, t, v, world, spec)
        if not bundle:
            ver["verdict"] = "invalid_task"
            break
        ver["bundle_sha256"] = bundle
        rollouts = evaluate(run, t, v, bundle)
        passes = sum(r["outcome"] == "VALID_PASS" for r in rollouts)
        ver["pattern"] = "".join("✓" if r["outcome"] == "VALID_PASS" else "✗" for r in rollouts)
        ver["passes"] = passes
        if len(rollouts) < N_RUNS:
            ver["verdict"] = "unresolved"
            break
        ver["verdict"] = "in_band" if 1 <= passes <= 3 else ("too_easy" if passes >= 4 else "too_hard")
        if ver["verdict"] != "too_easy" or v == 2:
            break
        chosen = run.chain.choose_mutations(passes, rollouts, None, history)
        entry = {"version": v + 1, "operators": chosen["operators"], "why": "; ".join(chosen["why"])}
        history.append(entry)
        world_input = {"origin": "mutation", "parent_world_sha256": world, "spec": spec,
                       "mutation": {"operators": chosen["operators"], "why": entry["why"]},
                       "mutations": list(history), "analysis": {},
                       "target_fix": chosen.get("target_fix")}
        world_kind = "world_mutation"
        ver["mutation"] = chosen


# --- the run ------------------------------------------------------------------------------


def specs_for(run_id: str, n: int, families: list[str]) -> list[dict]:
    existing: list[dict] = []
    for i in range(n):
        existing.append(sample_spec_v2(existing, area=families[i % len(families)], rng=random.Random(f"{run_id}-{i}")))
    return existing


def readiness() -> list[tuple[str, bool, str]]:
    """What --execute needs, checked without spending anything."""
    from shared.contracts import config  # noqa: F401  (shared/contracts/config.py loads the repo's .env)

    checks = []
    try:
        from generator import author  # noqa: F401
        checks.append(("generator/author.py run_world_job", hasattr(author, "run_world_job"), "TB2"))
    except ImportError as e:
        checks.append(("generator/author.py run_world_job", False, str(e)))
    image = os.environ.get("SDE_AUTHORING_IMAGE", "sde-authoring:dev")
    ok = subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0
    checks.append((f"authoring image {image}", ok, "docker build -f authoring/Dockerfile -t sde-authoring:dev ."))
    checks.append(("OPENROUTER_API_KEY", bool(os.environ.get("OPENROUTER_API_KEY")), "set it in .env"))
    return checks


def summarize(run: Run, tasks: list[dict], stopped: str | None) -> str:
    lines = [f"# P1 run `{run.run_id}`", "",
             f"Spend: **${run.ledger.spent():.2f}**" + (f" of ${run.budget:.2f}" if run.budget else "") +
             (f". **Stopped:** {stopped}" if stopped else ""), ""]
    lines += ["| task | family / mechanism | version | verdict | runs | world | bundle |", "|---|---|---|---|---|---|---|"]
    for rec in tasks:
        s = rec["spec"]
        for ver in rec["versions"]:
            lines.append(f"| {rec['task']} | {s['family']} / {s['mechanism']} | v{ver['v']} | "
                         f"{ver.get('verdict', 'pending')} | {ver.get('pattern', '')} | "
                         f"{(ver.get('world_sha256') or '')[:12]} | {(ver.get('bundle_sha256') or '')[:12]} |")
    lines += ["", "## Steps", "", "| step | outcome | cost | duration | adopted | error |", "|---|---|---|---|---|---|"]
    for sid, e in run.ledger.steps.items():
        lines.append(f"| {sid} | {e['outcome']} | ${e['cost_usd']:.4f} | {e['duration_sec']}s | "
                     f"{'yes' if e['adopted'] else ''} | {(e.get('error') or '')[:80]} |")
    by_kind: dict[str, float] = {}
    for e in run.ledger.steps.values():
        by_kind[e["kind"]] = by_kind.get(e["kind"], 0.0) + e["cost_usd"]
    lines += ["", "## Cost by step kind", ""] + [f"- {k}: ${c:.4f}" for k, c in sorted(by_kind.items())]
    return "\n".join(lines) + "\n"


def plan_text(run_id: str, specs: list[dict], mutation: bool, checks) -> str:
    base = sum(ESTIMATE[k][0] for k in ("world", "world_check", "create", "validate")) + N_RUNS * ESTIMATE["rollout"][0]
    worst_v = base + MAX_REPAIRS * (ESTIMATE["world_repair"][0] + ESTIMATE["repair"][0]) \
        + N_RUNS * MAX_ROLLOUT_RETRIES * ESTIMATE["rollout"][0]
    versions = 2 if mutation else 1
    minutes = sum(ESTIMATE[k][1] for k in ("world", "world_check", "create", "validate")) + N_RUNS * ESTIMATE["rollout"][1]
    n = len(specs)
    lines = [f"P1 dry run `{run_id}`: {n} task(s), {'with' if mutation else 'without'} a mutation round", ""]
    for i, s in enumerate(specs, 1):
        lines.append(f"  t{i:02d}: {s['family']} / {s['mechanism']} / {s['environment']} / "
                     f"{s['verification']} / shape {s['shape']} / dial {s['dial']}")
    lines += ["", "  per version: world → world_check → create → validate → 5 rollouts",
              f"  per version: ${base:.2f} with no repairs, ${worst_v:.2f} if every repair and retry is used; "
              f"~{minutes} min, steps in order"]
    if mutation:
        lines += [f"  the mutation round runs only when v1 is too easy (so far every generated task was):",
                  f"    likely   (v1 too easy, no repairs): ${2 * base:.2f}/task, ${2 * base * n:.2f} total, "
                  f"~{2 * minutes} min/task",
                  f"    lowest   (v1 in band, no repairs):  ${base:.2f}/task, ${base * n:.2f} total",
                  f"    ceiling  (every repair, both rounds): ${2 * worst_v:.2f}/task, ${2 * worst_v * n:.2f} total"]
    else:
        lines += [f"  estimated total: ${base * n:.2f} (no repairs) to ${worst_v * n:.2f} (every repair)"]
    lines += ["  (step costs are pre-P1 guesses: world $3.00, create $2.50, rollout $0.03; --budget-usd caps actual spend)",
              "", "  readiness for --execute:"]
    lines += [f"    [{'ok' if ok else 'MISSING'}] {name}{'' if ok else f'  ({hint})'}" for name, ok, hint in checks]
    lines += ["", "  Nothing was called. Run with --execute --budget-usd N to spend up to N."]
    return "\n".join(lines)


def main(argv: list[str] | None = None, chain: Chain | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    ap.add_argument("--run", default="p1-a", help="run id (a rerun of the same id resumes)")
    ap.add_argument("--tasks", type=int, default=3)
    ap.add_argument("--family", action="append", help=f"families to cycle through (default {P1_FAMILIES})")
    ap.add_argument("--mutation", action="store_true", help="one mutation round for a too-easy v1")
    ap.add_argument("--execute", action="store_true", help="actually run (paid); requires --budget-usd")
    ap.add_argument("--budget-usd", type=float)
    ap.add_argument("--work-dir", type=Path, default=Path(os.environ.get("SDE_WORK_DIR", ROOT / "work")))
    ap.add_argument("--out-dir", type=Path, default=ROOT / "output" / "p1")
    args = ap.parse_args(argv)
    if args.execute and (args.budget_usd is None or args.budget_usd <= 0):
        ap.error("--execute requires a positive --budget-usd")

    families = args.family or P1_FAMILIES
    specs = specs_for(args.run, args.tasks, families)
    if not args.execute:  # nothing is called, imported for paid work, or written
        print(plan_text(args.run, specs, args.mutation, readiness()))
        return 0

    run = Run(args.run, args.work_dir, args.out_dir / args.run, chain=chain or real_chain(),
              budget=args.budget_usd)
    tasks: list[dict] = []
    stopped = None
    try:
        for i, spec in enumerate(specs, 1):
            run_task(run, i, spec, args.mutation, tasks)
    except BudgetStop as e:
        stopped = str(e)
    summary = summarize(run, tasks, stopped)
    run.out.mkdir(parents=True, exist_ok=True)  # a stop before the first step wrote no ledger
    (run.out / "summary.md").write_text(summary)
    print(summary)
    return 2 if stopped else 0


if __name__ == "__main__":
    sys.exit(main())
