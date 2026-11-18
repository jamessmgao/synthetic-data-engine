"""Status values and coordination limits shared by every pipeline component (PRD §3)."""

import os
from enum import StrEnum


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    CALIBRATING = "CALIBRATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class Stage(StrEnum):
    """Stage of a task's current version."""

    QUEUED = "QUEUED"
    GENERATING = "GENERATING"
    BUILDING = "BUILDING"
    VALIDATING = "VALIDATING"
    EVALUATING = "EVALUATING"
    BAND_OK = "BAND_OK"
    OUT_OF_BAND = "OUT_OF_BAND"
    INVALID = "INVALID"
    UNRESOLVED = "UNRESOLVED"


class RejectReason(StrEnum):
    EXHAUSTED = "exhausted"
    INVALID = "invalid"
    INFRA_UNRESOLVED = "infra_unresolved"


class JobType(StrEnum):
    GENERATE = "generate"
    VALIDATE = "validate"
    ROLLOUT = "rollout"
    SMOKE = "smoke"  # `python -m shared.jobs.smoke` only; no worker claims it


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


TERMINAL_TASK_STATUSES = (TaskStatus.ACCEPTED, TaskStatus.REJECTED)
SCORED_OUTCOMES = (Outcome.VALID_PASS, Outcome.VALID_FAIL)

# Finish what's started: rollouts before validations before new drafts (PRD §6.11).
PRIORITY = {JobType.ROLLOUT: 30, JobType.VALIDATE: 20, JobType.GENERATE: 10, JobType.SMOKE: 0}
# Job types that hold a Docker slot while leased.
DOCKER_JOB_TYPES = (JobType.VALIDATE, JobType.ROLLOUT)
DOCKER_SLOTS = int(os.environ.get("SDE_DOCKER_SLOTS", "4"))

# Calibration limits (PRD §6.3–§6.6), shared so every component agrees.
MAX_VERSIONS = 3
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
