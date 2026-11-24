"""
A/B: does dropping the user message's fixed sections cost validity?

Each spec is drafted twice from the same system prompt: variant A is the new
short user message (idea/taxonomy + form + dial); variant B appends the
original example prompt's fixed sections (difficulty.fixed_sections). Each
draft is gated once with no repairs and no rollouts, so the comparison is the
first-validate gate pass rate (including the leak review) and draft tokens.

Resumable like the pilots: model calls are cached by job id, Harbor steps and
leak reviews are adopted from the trace dirs, finished records are skipped.

Usage: python experiments/prompt_ab.py [--run ab1] [--slots 2]
"""

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.contracts import config  # noqa: E402
from generator.worker import run_generate_job  # noqa: E402
from validator.gates import run_validate_job  # noqa: E402

SPECS = {
    "pipeline": {"domain": "Data pipelines", "task_type": "Debugging", "environment": "Python service",
                 "core_capability": "Consistency of aggregated results", "failure_mode": "Data corruption"},
    "go": {"domain": "Distributed systems", "task_type": "Debugging", "environment": "Go service",
           "core_capability": "Concurrency", "failure_mode": "Race condition"},
    "sqlite": {"domain": "Databases", "task_type": "Migration", "environment": "Python application with SQLite",
               "core_capability": "Consistency", "failure_mode": "Data corruption"},
    "c": {"domain": "Systems programming", "task_type": "Debugging", "environment": "C program built with make",
          "core_capability": "Memory and resource management", "failure_mode": "Crash"},
    "rules": {"domain": "Billing", "task_type": "Implementation", "environment": "Python service",
              "core_capability": "Correctness under interacting rules", "failure_mode": "Wrong results"},
}
FORM = {"rules": "many_rules"}
VARIANTS = {"A-short": False, "B-fixed": True}


def run_one(run_id: str, key: str, variant: str, fixed: bool, out: Path, docker: threading.Semaphore,
            log) -> dict:
    path = out / f"{key}-{variant}.json"
    if path.exists():
        return json.loads(path.read_text())
    spec = {"taxonomy": SPECS[key],
            "difficulty": {"form": FORM.get(key, "behavior_preserving"), "dial": 2, "fixed_sections": fixed}}
    rec: dict = {"spec_key": key, "variant": variant, "fixed_sections": fixed}
    gen_id = f"{run_id}-{key}-{variant}-generate"
    log(f"{key}/{variant}: drafting")
    gen = run_generate_job(gen_id, {"origin": "initial", "spec": spec})
    calls = gen["result"]["llm_calls"]
    rec.update(draft_outcome=gen["outcome"], draft_error=gen["error"], draft_cost_usd=gen["cost_usd"],
               draft_rounds=len(calls), prompt_tokens=sum(c["prompt_tokens"] for c in calls),
               completion_tokens=sum(c["completion_tokens"] for c in calls),
               reasoning_tokens=sum(c["reasoning_tokens"] for c in calls),
               schema_rejections=sum(1 for c in calls if c["errors"]),
               bundle_sha256=gen["result"].get("bundle_sha256"))
    if gen["outcome"] == "OK":
        with docker:
            val = run_validate_job(gen["result"]["bundle_path"],
                                   Path(config.WORK_DIR) / "traces" / f"{run_id}-{key}-{variant}-validate")
        gates = val["result"]["gates"]
        rec.update(validate_outcome=val["outcome"], failed_gate=val["failed_gate"], leak_cost_usd=val["cost_usd"],
                   gates={k: g["passed"] for k, g in gates.items()},
                   leaks=(gates.get("leak") or {}).get("leaks"), feedback=val["feedback"][:3000])
    log(f"{key}/{variant}: draft {rec['draft_outcome']}, validate {rec.get('validate_outcome')} "
        f"(failed gate: {rec.get('failed_gate')})")
    path.write_text(json.dumps(rec, indent=1) + "\n")
    return rec


def summarize(records: list[dict]) -> str:
    lines = ["| Variant | Drafts OK | First-validate OK | Failed gates | Median completion tokens | Draft cost |",
             "|---|---|---|---|---|---|"]
    for variant in VARIANTS:
        rs = [r for r in records if r["variant"] == variant]
        toks = sorted(r["completion_tokens"] for r in rs)
        failed = [r.get("failed_gate") or ("draft" if r["draft_outcome"] != "OK" else None) for r in rs]
        lines.append(f"| {variant} | {sum(r['draft_outcome'] == 'OK' for r in rs)}/{len(rs)} | "
                     f"{sum(r.get('validate_outcome') == 'OK' for r in rs)}/{len(rs)} | "
                     f"{', '.join(f for f in failed if f) or 'none'} | {toks[len(toks) // 2] if toks else '-'} | "
                     f"${sum(r['draft_cost_usd'] + (r.get('leak_cost_usd') or 0) for r in rs):.2f} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="ab1")
    ap.add_argument("--slots", type=int, default=2)
    args = ap.parse_args()
    out = ROOT / "experiments" / "results" / args.run
    out.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()

    def log(msg: str) -> None:
        with lock:
            line = f"[{time.strftime('%H:%M:%S')}] {msg}"
            print(line, flush=True)
            with open(out / "events.log", "a") as f:
                f.write(line + "\n")

    docker = threading.BoundedSemaphore(args.slots)
    jobs = [(k, v, fixed) for k in SPECS for v, fixed in VARIANTS.items()]
    with ThreadPoolExecutor(len(jobs)) as pool:
        records = list(pool.map(lambda j: run_one(args.run, *j, out, docker, log), jobs))
    table = summarize(records)
    (out / "summary.md").write_text(table + "\n")
    log("done\n" + table)


if __name__ == "__main__":
    main()
