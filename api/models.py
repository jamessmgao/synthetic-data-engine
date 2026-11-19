"""The API's request/response bodies. Status vocabularies live in shared.contracts.models."""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from shared.contracts.models import RejectReason, Stage, TaskStatus

TASK_ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,63}$"


class TaskSpec(BaseModel):
    """An explicit spec: v1 (`taxonomy` / `difficulty`) or v2 (a `family`, with any of
    the other v2 fields; missing ones are sampled within the family). Unknown keys are
    passed through to the generator unchanged."""

    model_config = {"extra": "allow"}

    taxonomy: dict[str, str] | None = None
    difficulty: dict | None = None
    family: str | None = None
    mechanism: str | None = None
    shape: str | None = None
    environment: str | None = None
    verification: str | None = None
    dial: int | None = Field(None, ge=1, le=3)
    seed: str | None = Field(None, min_length=1, max_length=20_000)
    # Free-form authoring requirements, one per line (generator/prompting.py reads them).
    design: list[str] | None = Field(None, max_length=40)

    @model_validator(mode="after")
    def _design_size(self):
        if self.design and sum(len(line) for line in self.design) > 20_000:
            raise ValueError("design is limited to 20 KB")
        return self


class Filters(BaseModel):
    """Limit sampling to an area (one problem family, or a list; GET /v1/families) and,
    optionally, one mechanism, environment or dial. Unknown values are a 422 that lists
    the valid ones."""

    area: str | list[str] | None = None
    mechanism: str | None = None
    environment: str | None = None
    dial: int | None = Field(None, ge=1, le=3)

    def filter_kwargs(self) -> dict:
        return {"area": self.area, "mechanism": self.mechanism, "environment": self.environment,
                "dial": self.dial}


class TaskRequest(Filters):
    """One task: from a free-text idea (it becomes the spec's seed), an explicit spec, a
    complete raw user prompt, or none of them (sampled, within the filters if given)."""

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


class BatchRequest(Filters):
    """N tasks under one batch id. Each task samples its own spec, spread across the
    taxonomy (or within `area` and the filters), with a prompt as every task's seed. A
    v1 spec applies to every task as given.

        {"count": 10, "area": "database_consistency"}
        {"count": 10, "area": ["compilers_parsers", "algorithmic_constraints"]}
        {"count": 10}                                   # the whole taxonomy"""

    count: int = Field(10, ge=1, le=50)
    batch_id: str | None = Field(None, pattern=TASK_ID_PATTERN)
    prompt: str | None = Field(None, min_length=1, max_length=20_000)
    spec: TaskSpec | None = None
    # The most the batch's jobs may spend; past it none of its paid jobs is claimed.
    budget_usd: float | None = Field(None, ge=0, le=10_000)
    # Start every task at create on a checked world from the pool (GET /v1/worlds), within
    # `area`, instead of building a world (docs/HILLCLIMBING.md "reuse worlds").
    reuse_worlds: bool = False

    @model_validator(mode="after")
    def _one_source(self):
        if self.prompt is not None and self.spec is not None:
            raise ValueError("give a prompt or a spec, not both")
        return self


class CancelRequest(BaseModel):
    """Optional body of POST …/cancel: who stopped it and why (shown in the verdict)."""

    by: str = Field("user", min_length=1, max_length=100)
    note: str = Field("", max_length=2000)


class BatchCreated(BaseModel):
    batch_id: str
    task_ids: list[str]
    created: bool  # False when this exact batch already existed


class BatchSummary(BaseModel):
    batch_id: str
    tasks: int
    accepted: int
    rejected: int
    cancelled: int = 0
    in_progress: int
    created_at: str
    dev: bool = False  # in SDE_DEV_BATCHES, or every recorded cost 0 (a mock run)


class TaskCreated(BaseModel):
    task_id: str
    created: bool  # False when this exact task already existed


class VersionSummary(BaseModel):
    """One version in the list view: how it was made and how it scored."""
    n: int
    origin: str | None  # initial | mutation | easier | repair_ambiguity | variant | …
    pass_count: int | None  # passes, once all agent runs are scored


class TaskSummary(BaseModel):
    id: str
    name: str | None
    status: TaskStatus
    # A cancelled task carries CANCELLED_REASON; a rejected one, a RejectReason.
    reason: RejectReason | Literal["cancelled_by_user"] | None
    current_version: int
    stage: Stage
    pass_pattern: str  # one char per rollout slot: ✓ pass, ✗ fail, · not yet scored
    cost_usd: float | None
    dev: bool = False  # recorded cost exactly 0 (mock runs); left out of reports by default
    created_at: str
    # Taxonomy and revision history (the UI's analytics); None on raw-prompt / pre-v2 tasks.
    family: str | None = None
    mechanism: str | None = None
    environment: str | None = None
    batch_id: str | None = None
    accepted_version: int | None = None
    reused_world: bool = False
    versions: list[VersionSummary] = []
