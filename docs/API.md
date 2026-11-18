# The API (`api/`)

A FastAPI app (`api/app.py:create_app`, served by `python -m api`) over the pipeline's store.
It creates tasks and batches, cancels them, and reports; it never runs a job (the services
pull their own work, docs/PRD.md §5). Responses come from `api/service.py`.

**Auth.** Every `/v1` route needs `X-Api-Key: $SDE_API_KEY`. No key configured on the server
→ 503 (closed, not open); a wrong key → 401. `/healthz` needs none.

**Errors.** Unknown task or batch → 404; the same id with a different request → 409;
invalid input (an unknown family, a bad dial, a negative budget) → 422 with the valid values.

## Creating work

### `POST /v1/tasks` → 202

One task. Give at most one of `prompt` (an idea, becomes the spec's seed), `spec` (explicit,
v1 or v2: missing v2 fields are sampled within the family), `raw_prompt` (the whole user
message, one-shot), or none (sampled). Optional filters: `area` (a family or a list),
`mechanism`, `environment`, `dial` (1-3); optional `task_id` (`^[a-z0-9][a-z0-9-]{0,63}$`).

```http
POST /v1/tasks
{"area": "database_consistency", "dial": 3}

202 {"task_id": "t-20261118-1a2b3c4d", "created": true}
```

The same `task_id` with the same request is a no-op (`created: false`). The first job is
`<task>-v1-world` in agent mode (a v2 spec), else `<task>-v1-create`
(`api/submit.py:first_job`).

### `POST /v1/batches` → 202

`count` (1-50, default 10) tasks under one `batch_id` (generated if absent), each with its
own spec spread across the taxonomy (or the area), plus the same filters, `prompt` or `spec`,
and **`budget_usd`** (0-10000, optional): the most the batch's jobs may spend.

```http
POST /v1/batches
{"count": 3, "batch_id": "p1", "budget_usd": 15,
 "area": ["distributed_concurrency", "database_consistency", "algorithmic_constraints"]}

202 {"batch_id": "p1", "task_ids": ["p1-t01", "p1-t02", "p1-t03"], "created": true}
```

One transaction: the batch exists whole or not at all. The same id with the same count,
request and budget is a no-op; anything else is a 409.

### The sampler (`shared/taxonomy/taxonomy.py:sample_spec_v2`)

Each task's spec is sampled against every task so far plus the batch's earlier picks:
**family first, weighted** (the family with the least `uses / FAMILY_WEIGHTS[family]`), then
within it an **unused (mechanism, environment) combination with the least-used values**, then
the family's least-used verification method; ties broken at random. A family repeats a
combination only after using all of its own; `dial` defaults to `DEFAULT_DIAL` (2). Filters
that no family in the area allows are a 422.

### `GET /v1/families`

Every family with what a request may name: `{name: {label, weight, mechanisms, environments,
verification, shapes}}` (`shared/taxonomy/taxonomy.py:families_listing`).

## Cancelling

`POST /v1/tasks/{task_id}/cancel` and `POST /v1/batches/{batch_id}/cancel`, optional body
`{"by": "user", "note": "…"}` (`api/service.py:cancel_task`, `cancel_batch` →
`shared/jobs/tasks.py:cancel`). Idempotent; decided tasks are left alone; a batch is cancelled in
one transaction.

```http
POST /v1/batches/b-1/cancel
{"note": "wrong area"}

200 {"batch_id": "b-1", "cancelled": 1, "already_decided": 2, "closed_jobs": 2,
     "tasks": {"b-1-t01": "ACCEPTED", "b-1-t02": "REJECTED", "b-1-t03": "CANCELLED"}}
```

A cancelled task is CANCELLED (stage CANCELLED, reason `cancelled_by_user`, with
`cancelled_at`, `cancelled_by`, `cancel_note`): no job of it is claimed again, its open jobs
are closed (DEAD, `cancelled: …`), a running job settles into nothing (no hand-off) but its
cost is still recorded, and any later reject, accept or compare-and-set is a no-op. Reports
count it under `cancelled`, never as a failure or in the yield denominator. The verdict reads
`CANCELLED by <by> at <time> (vN): <note>`. `scripts/relabel_cancelled.py --task-ids …
[--execute]` turns tasks stopped before cancel existed (REJECTED `infra_unresolved`) into
cancelled ones (dry run by default).

## Reading

| route | returns |
|---|---|
| `GET /v1/tasks?status=&since=` | every task's summary, oldest first |
| `GET /v1/tasks/{id}` | the verdict: `{task, versions: [{n, origin, operator, mutation_operators, state, world: {world_sha256, lineage, status, outcome}, bundle_sha256, repairs, gates, pass_count, pass_pattern, cost_usd}], rollouts: [{…, outcome, reward, failure, trace_uri, cost_usd}], decision, reason, cost_usd}` |
| `GET /v1/batches` | each batch: `{batch_id, tasks, accepted, rejected, cancelled, in_progress, created_at}` |
| `GET /v1/batches/{id}` | `{batch_id, counts_by_status, tasks: [...]}` |
| `GET /v1/batches/{id}/report` | the report over the batch, plus `budget` (the batch's) and `pipeline_budget` |
| `GET /v1/report?task_id=…&since=…` | the report over the named tasks (or those since a time, or all), plus `budget` (the pipeline's) |

### The report (`api/service.py:report`, `_summary`)

```json
{
  "task_ids": ["…"], "candidates": 8,
  "structural_pass": 7, "functional_pass": 6,
  "accepted_v1": 3, "accepted_total": 4,
  "rejected": {"invalid": 2, "exhausted": 1, "infra_unresolved": 1},
  "in_progress": 0, "cancelled": 0,
  "pass_count_histogram": {"0": 3, "1": 1, "2": 3, "3": 0, "4": 0, "5": 1},
  "valid_rollouts": 44, "invalid_rollouts": 5,
  "cost_usd": {"authoring": 9.1, "evaluation": 1.4, "total": 10.5,
               "authoring_per_task": 1.14, "evaluation_per_task": 0.18, "per_accepted": 2.63},
  "wall_clock_s": 5400,
  "config_hash": "…",
  "instruction_tokens": {"world": {"n": 8, "mean": 2086, "max": 2140},
                         "world_mutation": {"n": 2, "mean": 1485, "max": 1490},
                         "create": {"n": 8, "mean": 4324, "max": 4400}},
  "by_family": {"database_consistency": {"candidates": 2, "finished": 2, "accepted": 1, "rejected": 1,
                                         "learnable_rate": 0.5, "valid_rollouts": 10, "pass_rate": 0.4,
                                         "cost_usd": {"authoring": 2.1, "evaluation": 0.3,
                                                      "authoring_per_task": 1.05}}},
  "by_mechanism": {"…": {}},
  "yield": {"candidates": 8, "functional": 6, "accepted": 4, "accepted_v1": 3, "learnable_rate": 0.5},
  "budget": {"cap_usd": 50.0, "spent_usd": 10.5, "leased_estimate_usd": 4.0,
             "remaining_usd": 35.5, "budget_exhausted": false}
}
```

- **Authoring** cost is every job but the runs and their reviews (world, world_check,
  create / revise, validate and its leak review); **evaluation** is rollouts and analyze.
- `null` means not measured yet, never zero. `learnable_rate` = accepted / finished, where
  finished leaves out tasks in progress and cancelled ones.
- `instruction_tokens` is the rendered instruction's estimated size per authoring step
  (`result.authoring.instruction_size`).

### `GET /healthz`

```json
{"status": "ok | budget_exhausted", "db": "ok", "budget": {"cap_usd": null, "…": "…"},
 "backends": {"store": "sqlite | postgresql", "queue": "DbQueue | RedisQueue",
              "artifacts": "LocalArtifactStore | S3ArtifactStore"}}
```

503 if the database is unreachable. `budget_exhausted` is still HTTP 200.

## The spend cap (`shared/storage/budget.py`)

No batch or bug can run away with money. Two caps, both checked inside the claim
(`shared/storage/sql.py:claim`, via `claim_filter`), so every service obeys them:

- **`SDE_BUDGET_USD`**: the whole pipeline (unset or empty = no cap; negative is an error);
- **a batch's `budget_usd`** (POST /v1/batches), stored in the `batches` table.

A **paid** job is claimed only while

```
SUM(jobs.cost_usd) + an estimate for every OTHER leased job + this job's estimate  <=  cap
```

for the pipeline, and for the job's batch over that batch's jobs. Leased jobs count at their
estimate because cost is recorded only at settle; expired leases count too (the worker may
have spent before it died).

| job type | paid? | estimate (`ESTIMATE_USD`) |
|---|---|---|
| world (agent) | yes | $1.00 |
| create (agent / one-shot) | yes | $4.00 / $0.10 |
| revise (agent / one-shot) | yes | $3.00 / $0.10 |
| validate | one-shot only (its leak review); in agent mode it adopts the create job's review | $0.02 |
| rollout | yes | $0.03 |
| analyze | yes (the failure reviews) | $0.02 |
| world_check | no | - |

The agent estimates are about 2× P1's measurements (a world $0.51, a task $2.39).
Unpaid jobs keep moving past the cap; a held-back job stays READY (and in Redis) and is
claimed once the cap is raised. The claim runs inside a serialized store transaction
(SQLite `BEGIN IMMEDIATE`, Postgres `pg_advisory_xact_lock`), so concurrent workers can't
both take the last room. The cap can still be passed by a job costing more than its estimate.
When no paid job fits, `JobQueue.claim` logs `BUDGET EXHAUSTED` (at most once a minute), and
`/healthz` and the reports show it (`budget.status`).
