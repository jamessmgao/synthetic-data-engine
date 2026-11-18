"""The API's request/response bodies. Status vocabularies live in shared.contracts.models."""

from pydantic import BaseModel, Field, model_validator

from shared.contracts.models import RejectReason, Stage, TaskStatus

TASK_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class TaskSpec(BaseModel):
    """An explicit spec. Unknown keys are passed through to the generator unchanged."""

    model_config = {"extra": "allow"}

    taxonomy: dict[str, str] | None = None
    difficulty: dict | None = None
    # Free-form authoring requirements, one per line (generator/prompting.py reads them).
    design: list[str] | None = Field(None, max_length=40)

    @model_validator(mode="after")
    def _design_size(self):
        if self.design and sum(len(line) for line in self.design) > 20_000:
            raise ValueError("design is limited to 20 KB")
        return self


class TaskRequest(BaseModel):
    """One task: from a free-text idea, an explicit spec, a complete raw user prompt,
    or none of them (sampled)."""

    prompt: str | None = Field(None, min_length=1, max_length=20_000)
    spec: TaskSpec | None = None
    # The whole user message, verbatim (the system prompt and schema still apply).
    raw_prompt: str | None = Field(None, min_length=1, max_length=100_000)
    task_id: str | None = Field(None, pattern=TASK_ID_PATTERN)

    @model_validator(mode="after")
    def _one_source(self):
        if sum(x is not None for x in (self.prompt, self.spec, self.raw_prompt)) > 1:
            raise ValueError("give at most one of prompt, spec or raw_prompt")
        return self


class BatchRequest(BaseModel):
    """N tasks under one batch id. A prompt or spec applies to every task;
    with neither, each task samples its own spec, spread across the taxonomy."""

    count: int = Field(10, ge=1, le=50)
    batch_id: str | None = Field(None, pattern=TASK_ID_PATTERN)
    prompt: str | None = Field(None, min_length=1, max_length=20_000)
    spec: TaskSpec | None = None

    @model_validator(mode="after")
    def _one_source(self):
        if self.prompt is not None and self.spec is not None:
            raise ValueError("give a prompt or a spec, not both")
        return self


class BatchCreated(BaseModel):
    batch_id: str
    task_ids: list[str]
    created: bool  # False when this exact batch already existed


class BatchSummary(BaseModel):
    batch_id: str
    tasks: int
    accepted: int
    rejected: int
    in_progress: int
    created_at: str


class TaskCreated(BaseModel):
    task_id: str
    created: bool  # False when this exact task already existed


class TaskSummary(BaseModel):
    id: str
    name: str | None
    status: TaskStatus
    reason: RejectReason | None
    current_version: int
    stage: Stage
    pass_pattern: str  # one char per rollout slot: ✓ pass, ✗ fail, · not yet scored
    cost_usd: float | None
    created_at: str
