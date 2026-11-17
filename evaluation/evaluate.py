"""
Measure one task's difficulty: N_RUNS rollouts of the pinned agent, then the band verdict.

    python -m evaluation.evaluate output/<task> [--runs 5] [--workers 4]

A developer tool that stands in for the Job Service for a single task: it puts
the task into the artifact store as an immutable bundle, creates an ad-hoc task
row and its rollout jobs, runs executor workers until every slot has a valid
measurement or has used up its retries, prints the pass distribution and
writes ``records/evaluation/<task_id>/summary.json``.

Everything goes through the pipeline's real store and queue (``from_env``), so
other ``python -m evaluation.executor`` processes help with the same jobs, and
rerunning the command after a kill resumes: task and job ids are derived from
the bundle hash, finished jobs stay finished, and in-flight trials are adopted.
"""

import argparse
import json
import logging
import re
from pathlib import Path

from evaluation import harbor, settings
from evaluation.classify import INVALID, VALID_FAIL, VALID_PASS
from evaluation.executor import Executor, config_hash, rollout_job
from shared.storage import Backend, from_env
from shared.storage.sql import insert_job, utcnow
from shared.jobs.jobqueue import JobQueue
from shared.contracts.models import DOCKER_SLOTS, JobType

RECORDS_DIR = settings.ROOT_DIR / "records" / "evaluation"
BATCH_ID = "adhoc"


def task_id_for(task_dir: Path, sha: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", task_dir.name.lower()).strip("-")
    return f"{BATCH_ID}-{slug}-{sha[:8]}"


def submit(backend: Backend, task_dir: Path, runs: int) -> tuple[str, str]:
    """Store the bundle, create the task and its rollout jobs. Idempotent."""
    sha = backend.artifacts.put_bundle(task_dir)
    task_id = task_id_for(task_dir, sha)
    agent_hash = config_hash(harbor.pinned_agent())
    now = utcnow()
    with backend.store.tx() as c:
        c.execute(
            """INSERT INTO tasks (id, batch_id, status, stage, current_version, bundle_sha256, name,
                                  created_at, updated_at)
               VALUES (?, ?, 'CALIBRATING', 'EVALUATING', 1, ?, ?, ?, ?) ON CONFLICT DO NOTHING""",
            (task_id, BATCH_ID, sha, task_dir.name, now, now),
        )
        new = [j["id"] for slot in range(1, runs + 1)
               if insert_job(c, id=(j := rollout_job(task_id, 1, slot, sha))["id"], task_id=task_id,
                             version=1, type=JobType.ROLLOUT, slot=slot, input={"task_dir": str(task_dir)},
                             bundle_sha256=sha, config_hash=agent_hash)]
    JobQueue(backend).publish(new)
    return task_id, sha


def summarize(jobs: list[dict], runs: int) -> dict:
    """Per-slot outcome (the valid measurement if there is one) and the band verdict."""
    slots = {}
    for slot in range(1, runs + 1):
        attempts = sorted((j for j in jobs if j["type"] == JobType.ROLLOUT and j["slot"] == slot),
                          key=lambda j: j["retry"])
        valid = next((j for j in attempts if j.get("outcome") in (VALID_PASS, VALID_FAIL)), None)
        slots[slot] = {
            "outcome": valid["outcome"] if valid else (attempts[-1].get("outcome") if attempts else None),
            "reward": valid["reward"] if valid else None,
            "job_id": (valid or attempts[-1])["id"] if attempts else None,
            "attempts": [{"job_id": j["id"], "status": j["status"], "outcome": j.get("outcome"),
                          "error": j.get("error"), "adopted": (j.get("result") or {}).get("adopted"),
                          "cost_usd": j.get("cost_usd"), "trace_uri": j.get("trace_uri"),
                          "duration_sec": (j.get("result") or {}).get("duration_sec")} for j in attempts],
        }
    valid = [s for s in slots.values() if s["outcome"] in (VALID_PASS, VALID_FAIL)]
    passes = sum(1 for s in valid if s["outcome"] == VALID_PASS)
    if len(valid) < runs:
        verdict = "UNRESOLVED"
    elif settings.BAND_LOW <= passes <= settings.BAND_HIGH:
        verdict = "BAND_OK"
    else:
        verdict = "TOO_HARD" if passes < settings.BAND_LOW else "TOO_EASY"
    rollouts = [j for j in jobs if j["type"] == JobType.ROLLOUT]
    checksums = {(j.get("result") or {}).get("task_checksum") for j in rollouts if j.get("outcome") != INVALID}
    return {
        "valid_runs": len(valid),
        "passes": passes,
        "pass_pattern": "".join({VALID_PASS: "✓", VALID_FAIL: "✗"}.get(s["outcome"], "?") for s in slots.values()),
        "verdict": verdict,
        "band": [settings.BAND_LOW, settings.BAND_HIGH],
        "harbor_task_checksums": sorted(c for c in checksums if c),
        "invalid_attempts": sum(1 for j in rollouts if j.get("outcome") == INVALID),
        "cost_usd": round(sum(j.get("cost_usd") or 0 for j in rollouts), 6),
        "slots": slots,
    }


def evaluate(task_dir: Path, runs: int, workers: int, backend: Backend | None = None) -> dict:
    backend = backend or from_env()
    task_dir = task_dir.resolve()
    task_id, sha = submit(backend, task_dir, runs)

    def idle() -> bool:
        return all(j["status"] in ("DONE", "DEAD") for j in backend.store.task_jobs(task_id))

    Executor(backend, docker_slots=workers).serve(n_workers=workers, idle=idle)

    agent = harbor.pinned_agent()
    summary = {
        "task_id": task_id,
        "task_dir": str(task_dir),
        "bundle_sha256": sha,
        "agent": agent.as_expected(),
        "config_hash": config_hash(agent),
        **summarize(backend.store.task_jobs(task_id), runs),
    }
    out = RECORDS_DIR / task_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    summary["record"] = str(out / "summary.json")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0].strip())
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--runs", type=int, default=settings.N_RUNS)
    parser.add_argument("--workers", type=int, default=DOCKER_SLOTS)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    s = evaluate(args.task_dir, args.runs, args.workers)
    print(f"\n{s['task_id']}  {s['pass_pattern']}  {s['passes']}/{s['valid_runs']} valid passes  "
          f"→ {s['verdict']} (band {s['band'][0]}–{s['band'][1]})")
    for slot, info in s["slots"].items():
        tries = ", ".join(f"{a['outcome']}{' adopted' if a['adopted'] else ''}"
                          f"{' [' + a['error'] + ']' if a['error'] else ''}" for a in info["attempts"])
        print(f"  slot {slot}: {tries}")
    print(f"  invalid attempts: {s['invalid_attempts']}   cost: ${s['cost_usd']:.4f}   record: {s['record']}")


if __name__ == "__main__":
    main()
