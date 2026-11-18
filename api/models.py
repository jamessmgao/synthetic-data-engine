"""The API's request/response bodies. Status vocabularies live in shared.contracts.models."""

from pydantic import BaseModel, Field

from shared.contracts.models import RejectReason, Stage, TaskStatus

from api.submit import MAX_BATCH_COUNT

BATCH_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class BatchRequest(BaseModel):
    count: int = Field(10, ge=1, le=MAX_BATCH_COUNT)
    batch_id: str | None = Field(None, pattern=BATCH_ID_PATTERN)


class BatchSubmitted(BaseModel):
    batch_id: str
    task_ids: list[str]
    created: bool  # False when this exact batch already existed


class BatchListItem(BaseModel):
    batch_id: str
    tasks: int
    accepted: int
    rejected: int
    created_at: str


class TaskSummary(BaseModel):
    id: str
    name: str | None
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
