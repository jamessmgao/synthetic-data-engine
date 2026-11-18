"""Status values and coordination limits shared by every pipeline component (PRD §3)."""

import os
from enum import StrEnum


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    CALIBRATING = "CALIBRATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"  # stopped by a user (POST …/cancel): decided, but neither yield nor failure


class Stage(StrEnum):
    """Stage of a task's current version."""

    WORLD_BUILDING = "WORLD_BUILDING"  # agent mode: a world job builds (or mutates) the world
    WORLD_CHECKING = "WORLD_CHECKING"  # agent mode: world_check gates it
    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    BUILDING = "BUILDING"
    VALIDATING = "VALIDATING"
    EVALUATING = "EVALUATING"
    BAND_OK = "BAND_OK"
    OUT_OF_BAND = "OUT_OF_BAND"
    INVALID = "INVALID"
    UNRESOLVED = "UNRESOLVED"
    CANCELLED = "CANCELLED"


class RejectReason(StrEnum):
    EXHAUSTED = "exhausted"
    INVALID = "invalid"
    INFRA_UNRESOLVED = "infra_unresolved"


class JobType(StrEnum):
    """One queue per type (Redis key `sde:<type>`), PRD §5.1."""

    WORLD = "world"  # Generation Service, agent mode: build, repair or mutate a world
    CREATE = "create"  # Generation Service: a new task from its spec, or (agent) from a world
    REVISE = "revise"  # Generation Service: a repair or a revision (the revise queue)
    WORLD_CHECK = "world_check"  # Build + Validate, agent mode: the world's repro fails, its fix holds
    VALIDATE = "validate"  # Build + Validate
    ROLLOUT = "rollout"  # Executor (the run queue)
    ANALYZE = "analyze"  # Trace Parser: accept / revise / reject
    SMOKE = "smoke"  # `python -m shared.jobs.smoke` only; no worker claims it


# The Generation Service's queues, revisions first. World jobs exist in agent mode only.
GENERATION_TYPES = (JobType.REVISE, JobType.CREATE)
AGENT_GENERATION_TYPES = (*GENERATION_TYPES, JobType.WORLD)
# Build + Validate's queues.
GATE_TYPES = (JobType.VALIDATE, JobType.WORLD_CHECK)


class JobStatus(StrEnum):
    READY = "READY"
    LEASED = "LEASED"
    DONE = "DONE"
    DEAD = "DEAD"


class Outcome(StrEnum):
    OK = "OK"
    FAILED = "FAILED"
    VALID_PASS = "VALID_PASS"
    VALID_FAIL = "VALID_FAIL"
    INVALID = "INVALID"


TERMINAL_TASK_STATUSES = (TaskStatus.ACCEPTED, TaskStatus.REJECTED, TaskStatus.CANCELLED)
# SQL for "the task is decided": no job of such a task is ever claimed.
TERMINAL_SQL = "(" + ", ".join(f"'{s}'" for s in TERMINAL_TASK_STATUSES) + ")"
CANCELLED_REASON = "cancelled_by_user"
SCORED_OUTCOMES = (Outcome.VALID_PASS, Outcome.VALID_FAIL)

# Finish what's started: decisions, rollouts, validations, revisions, new drafts (PRD §6.11).
PRIORITY = {
    JobType.ANALYZE: 40,
    JobType.ROLLOUT: 30,
    JobType.VALIDATE: 20,
    JobType.WORLD_CHECK: 18,
    JobType.REVISE: 15,
    JobType.CREATE: 12,  # a task whose world is checked, before starting a new world
    JobType.WORLD: 10,
    JobType.SMOKE: 0,
}
# How tasks are authored (docs/AGENTIC_AUTHORING_PLAN.md §1):
#   oneshot  create / revise: one GLM-5.1 call per draft (generator/worker.py:run_generate_job)
#   agent    world → world_check → create → validate. world, create and revise are each a
#            Harbor authoring trial, terminus-2 + GLM-5.1 for up to ~60 min
#            (generator/author.py: run_world_job, run_create_job), holding a Docker slot
AUTHOR_MODES = ("oneshot", "agent")
AUTHOR_MODE = os.environ.get("SDE_AUTHOR_MODE", "oneshot")
if AUTHOR_MODE not in AUTHOR_MODES:
    raise ValueError(f"SDE_AUTHOR_MODE must be one of {AUTHOR_MODES}, not {AUTHOR_MODE!r}")

# Docker slots are decided per JOB, when it is queued (jobs.needs_docker): these types
# always run a container; create / revise only when authored from a world (an agent
# trial), never as a one-shot GLM call. Agent jobs (world, and create / revise from a
# world) hold their slot for up to an hour, so at most SDE_AGENT_SLOTS of a machine's
# SDE_DOCKER_SLOTS go to them: the rest stay free for the gates and rollouts.
DOCKER_JOB_TYPES = (*GATE_TYPES, JobType.ROLLOUT, JobType.WORLD)
AGENT_JOB_TYPES = AGENT_GENERATION_TYPES
DOCKER_SLOTS = int(os.environ.get("SDE_DOCKER_SLOTS", "4"))
AGENT_SLOTS = int(os.environ.get("SDE_AGENT_SLOTS") or max(1, DOCKER_SLOTS - 1))


def needs_docker(type: str, input: dict | None) -> bool:
    if type in DOCKER_JOB_TYPES:
        return True
    return type in GENERATION_TYPES and bool((input or {}).get("world_sha256"))

# Calibration limits (PRD §6.3–§6.6), shared so every component agrees.
# Versions per task: v1 plus up to MAX_VERSIONS - 1 revisions by the Trace Parser.
MAX_VERSIONS = int(os.environ.get("SDE_MAX_VERSIONS", "8"))
MAX_REPAIRS_PER_VERSION = 2
MAX_ROLLOUT_RETRIES = 3
# Claims of one job row before the reconciler dead-letters it: a lease that keeps
# expiring means the job keeps killing its worker.
MAX_JOB_ATTEMPTS = 3

# Lease timing. Tests shorten these so kill/resume runs in seconds.
LEASE_TTL_S = float(os.environ.get("SDE_LEASE_TTL_S", "600"))
HEARTBEAT_S = float(os.environ.get("SDE_HEARTBEAT_S", "60"))
POLL_S = float(os.environ.get("SDE_POLL_S", "2"))
RECONCILE_S = float(os.environ.get("SDE_RECONCILE_S", "30"))
