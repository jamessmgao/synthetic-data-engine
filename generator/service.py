"""The Generation Service: create, revise and (agent mode) world jobs, on the generic loop
of shared/jobs/worker.py.

The job functions are `generator/worker.py:run_generate_job` (SDE_AUTHOR_MODE=oneshot), or
`generator/author.py:run_world_job / run_create_job` (agent). The adapters below only
translate between a job row and those functions' arguments and results, and move artifacts
between machines.

    python -m generator.service [generation] [--concurrency 2] [--with-reconciler]

It claims from the revise queue before the create queue, so started tasks finish before
new ones are drafted. (`generate` is accepted as an alias for `generation`.)
"""

import argparse
import json
import logging
import os
import threading
from typing import Any

from shared.contracts.models import (
    AGENT_GENERATION_TYPES,
    AGENT_SLOTS,
    AUTHOR_MODE,
    DOCKER_SLOTS,
    GENERATION_TYPES,
    JobType,
)
from shared.jobs import handoffs
from shared.jobs.jobqueue import JobQueue
from shared.jobs.worker import JobFn, JobFns, dispatch, as_job_fn, run_service, serve, settled as _settled
from shared.storage import Backend
from shared.taskformat.leak import leak_review

log = logging.getLogger("worker")

# Agent jobs are long authoring trials (time-boxed at 30 min for a world, 20 for a task,
# plus the build): a lease long enough to ride out a database
# outage, so a transient error never makes another worker re-run (and re-pay) one.
AGENT_LEASE_TTL_S = float(os.environ.get("SDE_AGENT_LEASE_TTL_S", "1800"))


def serve_generation(backend: Backend, fn: JobFns, **kw: Any) -> None:
    """The Generation Service: revise, then create, then (agent mode) world jobs. In
    agent mode a world job, and a create / revise from a world, is a Harbor authoring
    trial holding a Docker slot; at most AGENT_SLOTS of the machine's slots go to them,
    so gates and rollouts are never starved. One-shot drafts hold no slot (the slot is
    decided per job: jobs.needs_docker). `fn` is one function for every type, or one
    per type."""
    agent = AUTHOR_MODE == "agent"
    types = AGENT_GENERATION_TYPES if agent else GENERATION_TYPES
    if agent:
        kw.setdefault("docker_slots", DOCKER_SLOTS)
        kw.setdefault("agent_slots", AGENT_SLOTS)
        kw.setdefault("lease_sec", AGENT_LEASE_TTL_S)
        kw.setdefault("on_lost", stop_authoring_trial)  # a cancel stops the running trial
    drafts = {"handoff": handoffs.after_generation(backend), "start": handoffs.generation_started(backend)}
    world = {"handoff": handoffs.after_world(backend), "start": handoffs.world_started(backend)}
    per_type = {t: (world if t == JobType.WORLD else drafts) for t in types}
    serve(JobQueue(backend), types, as_job_fn(fn),
          handoff=dispatch({t: h["handoff"] for t, h in per_type.items()}),
          on_start=dispatch({t: h["start"] for t, h in per_type.items()}), **kw)


# --- adapters to the job functions ----------------------------------------------------


def stop_authoring_trial(job: dict[str, Any]) -> None:
    """A lost agent job (cancelled, or taken over): stop its Harbor trial now
    (generator/authoring_harness.py:stop_trial), as the Executor does for rollouts."""
    from generator import authoring_harness

    if authoring_harness.stop_trial(job["id"]):
        log.warning("%s: lease lost; its authoring trial is being stopped", job["id"])


def _save_trial(backend: Backend, job_id: str) -> str | None:
    """An agent job's authoring trial lives in its trace dir: save it, so a machine
    that reclaims or retries the job adopts it (fetched before the run) instead of
    paying for it again, and the audit keeps it. Called in a `finally`: a job whose
    authoring function raised is released and may be retried on another machine."""
    if backend.artifacts.trace_dir(job_id).exists():
        return backend.artifacts.save_trace(job_id)
    return None


EARLIER_TASKS_MAX = 3  # earlier tasks on a reused world shown to its new task's author
EARLIER_INSTRUCTION_CHARS = 1500  # each one's instruction, cut (an instruction is ~150 words)


def earlier_instructions(backend: Backend, world_sha: str, task_id: str) -> list[str]:
    """The instructions of the other tasks written on this world (each task's latest bundle,
    the most recent tasks first, at most EARLIER_TASKS_MAX): a reused world's new task must
    not repeat them. A bundle that can't be fetched is skipped."""
    with backend.store.tx() as conn:
        rows = conn.execute(
            "SELECT task_id, bundle_sha256, result FROM jobs WHERE type IN (?, ?) AND outcome = 'OK'"
            " AND bundle_sha256 IS NOT NULL AND task_id <> ? AND result LIKE ? ORDER BY finished_at DESC",
            (str(JobType.CREATE), str(JobType.REVISE), task_id, f"%{world_sha}%")).fetchall()
    bundles: dict[str, str] = {}
    for r in rows:
        result = r["result"] if isinstance(r["result"], dict) else json.loads(r["result"] or "{}")
        if result.get("world_sha256") == world_sha and r["task_id"] not in bundles:
            bundles[r["task_id"]] = r["bundle_sha256"]
    out = []
    for sha in list(bundles.values())[:EARLIER_TASKS_MAX]:
        try:
            text = (backend.artifacts.bundle_path(sha) / "instruction.md").read_text().strip()
        except OSError:
            continue
        out.append(text if len(text) <= EARLIER_INSTRUCTION_CHARS else text[:EARLIER_INSTRUCTION_CHARS] + " […]")
    return out


def draft_fn(backend: Backend, mode: str = AUTHOR_MODE) -> JobFn:
    """create / revise, chosen per job: a job from a world (input.world_sha256, agent mode)
    runs run_create_job (plan §3.4: run_generate_job's result shape plus world_sha256);
    any other job runs one-shot run_generate_job, even in agent mode. Raw prompts, v1
    specs and tasks in flight when the mode switched have no world.

    A job may run on another machine than the one that wrote its inputs, so the
    parent bundle, its draft record and the world are made local first; the new
    bundle and draft record are registered after."""
    from generator.worker import run_generate_job  # generator/worker.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        inp = job["input"] or {}
        agent = mode == "agent" and bool(inp.get("world_sha256"))
        if agent:
            from generator.author import run_create_job as draft  # generator/author.py
        else:
            draft = run_generate_job
        if parent := inp.get("bundle_sha256"):
            backend.artifacts.bundle_path(parent)  # downloads it in S3 mode
            backend.artifacts.fetch_file(f"drafts/{parent}.json")
        if world := inp.get("world_sha256"):
            backend.artifacts.world_path(world)
        if agent and (inp.get("reused") or inp.get("variant_of")) and inp.get("origin") == "initial":
            # A variant's create: the tasks already written on the world it is a variant of.
            task_id = job.get("task_id") or job["id"].rsplit("-v", 1)[0]
            source = inp.get("variant_of") or inp["world_sha256"]
            inp = {**inp, "earlier_instructions": earlier_instructions(backend, source, task_id)}
        if agent:
            backend.artifacts.fetch_trace(job["id"])
        trace_uri = None
        try:
            out = draft(job["id"], inp, backend.work_dir)
        finally:
            if agent:
                trace_uri = _save_trial(backend, job["id"])
        fields = _settled(out)
        if sha := (out.get("result") or {}).get("bundle_sha256"):
            backend.artifacts.register_bundle(sha)
            for rel in (f"drafts/{sha}.json", leak_review(sha)):
                if (backend.work_dir / rel).is_file():
                    backend.artifacts.register_file(rel)
            fields["bundle_sha256"] = sha
        if trace_uri:
            fields["trace_uri"] = trace_uri
        return fields

    return run


def world_fn(backend: Backend) -> JobFn:
    """world (agent mode): build a world, repair one (input.world_sha256) or mutate
    one (input.parent_world_sha256) with run_world_job; register the new world."""
    from generator.author import run_world_job  # generator/author.py

    def run(job: dict[str, Any]) -> dict[str, Any]:
        inp = job["input"] or {}
        for key in ("world_sha256", "parent_world_sha256"):
            if inp.get(key):
                backend.artifacts.world_path(inp[key])  # downloads it in S3 mode
        if inp.get("origin") == "variant" and inp.get("parent_world_sha256"):
            # The variant must not lead back to the tasks already written on its source world.
            task_id = job.get("task_id") or job["id"].rsplit("-v", 1)[0]
            inp = {**inp, "earlier_instructions": earlier_instructions(backend, inp["parent_world_sha256"], task_id)}
        backend.artifacts.fetch_trace(job["id"])
        try:
            out = run_world_job(job["id"], inp, backend.work_dir)
        finally:
            trace_uri = _save_trial(backend, job["id"])
        fields = _settled(out)
        if sha := (out.get("result") or {}).get("world_sha256"):
            backend.artifacts.register_world(sha)
        if trace_uri:
            fields["trace_uri"] = trace_uri
        return fields

    return run


def generation_fns(backend: Backend, mode: str = AUTHOR_MODE) -> dict[str, JobFn]:
    drafts = draft_fn(backend, mode)
    fns = {JobType.CREATE: drafts, JobType.REVISE: drafts}
    if mode == "agent":
        fns[JobType.WORLD] = world_fn(backend)
    return fns


def generate_fn(backend: Backend, mode: str = AUTHOR_MODE) -> JobFn:
    """One function for every Generation Service job type (dispatches on the type)."""
    return dispatch(generation_fns(backend, mode))


def start(backend: Backend, args: argparse.Namespace, stop: threading.Event) -> None:
    log.info("author mode: %s", AUTHOR_MODE)
    # Threads, not slots: one-shot drafts are model calls (no Docker slot), and agent
    # jobs are capped by the Docker / agent slot counts in the claim itself.
    default = int(os.environ.get("SDE_GENERATION_CONCURRENCY", "8"))
    serve_generation(backend, generation_fns(backend), concurrency=args.concurrency or default, stop=stop)


def main() -> None:
    run_service("Run the Generation Service.", ["generation", "generate"], start)


if __name__ == "__main__":
    main()
