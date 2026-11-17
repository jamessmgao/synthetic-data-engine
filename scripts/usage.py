"""
Sum the LLM spend recorded on disk. The gateway exposes no usage endpoint, so
this is our own ledger; the authoritative balance comes from synthetic-data-engine.

    glm-5.1        work/llm/*.json (one file per call; cached reuses are not re-counted)
                   records/generation/*/attempt-*.json (runs before the work/ store)
    glm-5.3-flash  agent_result.cost_usd in every Harbor trial result.json
                   under work/traces/ and jobs/

Usage: python scripts/usage.py [repo_dir ...]
"""

import json
import sys
from pathlib import Path


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def summarize(root: Path) -> tuple[float, int, float, int]:
    gen_cost, gen_calls = 0.0, 0
    for p in root.glob("work/llm/*.json"):
        gen_cost += load(p).get("completion", {}).get("cost_usd") or 0
        gen_calls += 1
    for p in root.glob("records/generation/*/attempt-*.json"):
        gen_cost += load(p).get("cost_usd") or 0
        gen_calls += 1

    agent_cost, trials = 0.0, 0
    for pattern in ("work/traces/**/result.json", "jobs/**/result.json"):
        for p in root.glob(pattern):
            r = load(p)
            if "agent_result" not in r:  # job-level summaries, not trials
                continue
            agent_cost += (r.get("agent_result") or {}).get("cost_usd") or 0
            trials += 1
    return gen_cost, gen_calls, agent_cost, trials


if __name__ == "__main__":
    roots = [Path(a) for a in sys.argv[1:]] or [Path(__file__).resolve().parent.parent]
    total = 0.0
    for root in roots:
        g, gc, a, t = summarize(root)
        total += g + a
        print(f"{root}")
        print(f"  glm-5.1 authoring      ${g:8.4f}  ({gc} calls)")
        print(f"  glm-5.3-flash agents   ${a:8.4f}  ({t} trials, incl. oracle/nop at $0)")
    print(f"total recorded           ${total:8.4f}")
