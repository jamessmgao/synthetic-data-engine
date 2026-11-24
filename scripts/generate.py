"""
Standalone generation loop: one prompt in, one validated task out.

This is `sde generate` from PRD §4 (one draft + gates, no evaluation), run
without the Job Service: it calls the same two job functions the pipeline
schedules, in order, and stops at the first valid version:

    generate (initial) -> validate -> [generate (repair) -> validate] x MAX_REPAIRS

Artifacts go to the shared store (config.WORK_DIR): bundles/, drafts/, llm/,
traces/<job_id>/. A summary of the run is written to runs/<run_id>.json, and a
valid task is copied to output/<name>/.

Usage: python scripts/generate.py <prompt-file | prompt text | spec.json>
       (or python -m scripts.generate ...)
"""

import argparse
import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

if not __package__:  # run by path (python scripts/generate.py): the repo root goes on the import path, in place of this directory
    _here = Path(__file__).resolve().parent
    sys.path[:] = [str(_here.parent)] + [p for p in sys.path if p != str(_here)]

from shared.contracts import config  # noqa: E402
from generator.worker import run_generate_job  # noqa: E402
from validator.gates import run_validate_job  # noqa: E402


def _new_run_id() -> str:
    return "g-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:6]


def generate_task(job_input: dict, run_id: str | None = None, output_root: str | None = None) -> dict:
    """Generate and validate one task, repairing gate failures up to MAX_REPAIRS times.

    Returns the run summary: {"status": "valid" | "invalid" | "failed", "task_dir", "steps", "cost_usd", ...}.
    """
    run_id = run_id or _new_run_id()
    work_dir = Path(config.WORK_DIR)
    started = time.monotonic()
    steps: list[dict] = []
    summary: dict = {"run_id": run_id, "status": "failed", "task_dir": None, "steps": steps}

    def finish() -> dict:
        summary["cost_usd"] = round(sum(s.get("cost_usd", 0) for s in steps), 6)
        summary["wall_time_sec"] = round(time.monotonic() - started, 1)
        path = work_dir / "runs" / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, indent=2) + "\n")
        summary["summary_path"] = str(path)
        return summary

    gen_input = job_input
    for n in range(config.MAX_REPAIRS + 1):
        gen_id = f"{run_id}-v1-generate-r0" if n == 0 else f"{run_id}-v1-repair{n}"
        _log(f"{gen_id}: drafting ({gen_input['origin']})")
        gen = run_generate_job(gen_id, gen_input, work_dir)
        steps.append({"job_id": gen_id, "type": "generate", **gen})
        if gen["outcome"] != "OK":
            summary["error"] = gen["error"]
            return finish()

        sha = gen["result"]["bundle_sha256"]
        val_id = f"{run_id}-v1-validate{n}"
        _log(f"{val_id}: validating bundle {sha[:12]}")
        val = run_validate_job(gen["result"]["bundle_path"], work_dir / "traces" / val_id)
        steps.append({"job_id": val_id, "type": "validate", "bundle_sha256": sha, **val})
        gates = ", ".join(f"{k} {'ok' if g['passed'] else 'FAIL'}" for k, g in val["result"]["gates"].items())
        _log(f"{val_id}: {gates}")
        summary.update(bundle_sha256=sha, name=gen["result"]["name"])
        if val["outcome"] == "OK":
            summary["status"] = "valid"
            summary["task_dir"] = str(_publish(gen["result"]["bundle_path"], gen["result"]["name"], sha, output_root))
            return finish()
        gen_input = {"origin": "repair", "bundle_sha256": sha, "feedback": val["feedback"]}

    summary["status"] = "invalid"
    summary["error"] = f"still failing gate {steps[-1]['failed_gate']!r} after {config.MAX_REPAIRS} repairs"
    return finish()


def _publish(bundle_path: str, name: str, sha: str, output_root: str | None) -> Path:
    dest = Path(output_root or config.OUTPUT_DIR) / name
    if dest.exists():
        dest = dest.with_name(f"{name}-{sha[:8]}")
    if not dest.exists():
        shutil.copytree(bundle_path, dest)
    return dest


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def _job_input_from_arg(arg: str) -> dict:
    if os.path.isfile(arg):
        text = Path(arg).read_text()
        if arg.endswith(".json"):
            return {"origin": "initial", "spec": json.loads(text)}
        return {"origin": "initial", "prompt": text}
    return {"origin": "initial", "prompt": arg}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate one validated Harbor task.")
    parser.add_argument("prompt", help="A prompt file (.md/.txt), a spec file (.json), or prompt text.")
    parser.add_argument("--output-dir", help="Where valid tasks are copied (default: output/).")
    args = parser.parse_args()

    res = generate_task(_job_input_from_arg(args.prompt), output_root=args.output_dir)
    print(f"status: {res['status']}  cost: ${res['cost_usd']:.4f}  time: {res['wall_time_sec']}s")
    print(f"summary: {res['summary_path']}")
    if res.get("task_dir"):
        print(f"task:    {res['task_dir']}")
    if res.get("error"):
        print(f"error:   {res['error']}")
    sys.exit(0 if res["status"] == "valid" else 1)
