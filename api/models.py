"""
Status vocabularies (docs/PRD.md §3) and the API's request/response bodies.

Statuses are stored as text and validated here, so adding one is a code
change, not a migration.
"""

from enum import StrEnum

from pydantic import BaseModel, Field

BATCH_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"
MAX_BATCH_COUNT = 50


class TaskStatus(StrEnum):
    CREATED = "CREATED"
    CALIBRATING = "CALIBRATING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


class RejectReason(StrEnum):
    EXHAUSTED = "exhausted"
    INVALID = "invalid"
    INFRA_UNRESOLVED = "infra_unresolved"


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


class JobType(StrEnum):
    GENERATE = "generate"
    VALIDATE = "validate"
    ROLLOUT = "rollout"


# Finish what's started: rollouts before validations before new drafts.
JOB_PRIORITY = {JobType.ROLLOUT: 30, JobType.VALIDATE: 20, JobType.GENERATE: 10}


class JobStatus(StrEnum):
    READY = "READY"
    LEASED = "LEASED"
    DONE = "DONE"
    DEAD = "DEAD"


class Outcome(StrEnum):
    # rollout jobs
    VALID_PASS = "VALID_PASS"
    VALID_FAIL = "VALID_FAIL"
    INVALID = "INVALID"
    # generate / validate jobs
    OK = "OK"
    FAILED = "FAILED"


SCORED_OUTCOMES = (Outcome.VALID_PASS, Outcome.VALID_FAIL)


class BatchRequest(BaseModel):
    count: int = Field(10, ge=1, le=MAX_BATCH_COUNT)
    batch_id: str | None = Field(None, pattern=BATCH_ID_PATTERN)


class BatchSubmitted(BaseModel):
    batch_id: str
    task_ids: list[str]
    created: bool  # False when this exact batch already existed


class TaskSummary(BaseModel):
    id: str
    status: TaskStatus
    reason: RejectReason | None
    current_version: int
    stage: Stage
    pass_pattern: str  # one char per rollout slot: ✓ pass, ✗ fail, · not yet scored
    cost_usd: float | None


class BatchStatus(BaseModel):
    batch_id: str
    counts_by_status: dict[str, int]
    tasks: list[TaskSummary]
