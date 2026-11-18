# PRD: Calibrated Task Generation Pipeline

Status: draft, 2026-11-17. Written in a system-design format: requirements, core entities,
API, high-level design, deep dives. Source material:
[`TASK_GENERATION_RULES.md`](TASK_GENERATION_RULES.md) (difficulty levers and taxonomy, folded
into §6.2 and §6.9), [`agent-evaluation-system_calibration_v3.excalidraw`](../agent-evaluation-system_calibration_v3.excalidraw)
(the target architecture), and [`shared/taskformat/schema.py`](../shared/taskformat/schema.py) (the authoring
contract). Items marked **(open)** still need a decision; see §7.

**One-line summary:** GLM-5.1 writes original Terminal-Bench 2 / Harbor tasks. The pipeline
proves each one works (the reference solution passes, an untouched container fails), then runs
`terminus-2` + `glm-5.3-flash` five times. It ships only tasks where 1–3 runs pass, and explains
every failure. Tasks outside the band are revised, up to `MAX_VERSIONS` versions (8), then rejected. Everything is recorded so a reviewer
can check each verdict without reading code, and the pipeline survives being killed or run twice.

---

## As built (2026-11-17): agent mode

The requirements and design below are unchanged. This section records what was built on
top of them, with the code that implements each part; where a later section describes only
one-shot generation, this is the current behavior. Contracts and details:
[`AGENTIC_AUTHORING_PLAN.md`](AGENTIC_AUTHORING_PLAN.md) (job types, inputs, status),
[`WORKER_INTERFACE.md`](WORKER_INTERFACE.md) (every job function's inputs and results),
[`VALIDATION_GATES.md`](VALIDATION_GATES.md) (validate and world_check gates). Method:
[`WORLD_CONSTRUCTION.md`](WORLD_CONSTRUCTION.md); review findings:
[`ARCHITECTURE_REVIEW.md`](ARCHITECTURE_REVIEW.md).

**Two authoring modes, chosen per job.** `SDE_AUTHOR_MODE=oneshot` (default) drafts a bundle
with one strict-JSON GLM-5.1 call (`generator/worker.py:run_generate_job`, §6.1).
`SDE_AUTHOR_MODE=agent` runs terminus-2 + GLM-5.1 sessions in the `sde-authoring` image:
`generator/author.py:run_world_job` builds a **world** (an executable system, its
invariants, a reproducible counterexample, a fix and a plausible wrong fix), and
`generator/author_task.py:run_create_job` writes a task **from that world**. In agent mode
`shared/jobs/worker.py:draft_fn` picks per job: a create or revise whose input has
`world_sha256` runs the agent author, and anything else (raw prompts, v1 specs, tasks in
flight when the mode switched) runs one-shot.

**Job types.** `world` and `world_check` join `create`, `revise`, `validate`, `rollout` and
`analyze` (`shared/contracts/models.py:JobType`; priorities analyze 40 > rollout 30 > validate 20 >
world_check 18 > revise 15 > create 12 > world 10). The agent-mode flow, with hand-offs in
`shared/jobs/handoffs.py`:

```
world ─► world_check ─fail─► world-repair<k> (≤ MAX_REPAIRS_PER_VERSION = 2) ─► REJECTED invalid
              │ ok                                    (after_world, after_world_check)
              ▼
create (from the world) ─► validate ─fail─► repair<k> (≤2; also when create's own leak review fails)
              │ ok                         (after_generation, after_validate; the world travels along)
              ▼
rollout ×5 ─► analyze ─► accept | world mutation (v+1) | revise "easier" | revise "repair_ambiguity" | reject
```

Task stages gain `WORLD_BUILDING` and `WORLD_CHECKING` (`shared/contracts/models.py:Stage`).

**Gates.**
- `validate` (`validator/gates.py:run_validate_job`): structural · lint · oracle ×2 · nop ·
  **wrong_fix** (`gate_wrong_fix`: the bundle's plausible wrong fix, run as the oracle,
  must score 0) · **leak** (`gate_leak`: a semantic review against the hidden spec and the
  world, plus a blind fix prediction, `SDE_LEAK_BLIND`).
- `create` runs the leak review first (no Docker slot). A leaking draft fails with
  `failure_kind: author` and goes to a repair, not a rejection.
- `world_check` (`shared/taskformat/world_check.py:run_world_check_job`): the world builds; the
  repro fails twice; it passes twice with the fix.
- Its **hardness** checks (the symptom is visible; the fix isn't one local patch, i.e. 2+
  files or a dependent the wrong fix breaks; the wrong fix clears the symptom and fails
  the repro) follow `SDE_WORLD_HARDNESS` = `off` | `warn` (default: recorded in
  `result.hardness`, not failing) | `enforce` | `strict`. Details: docs/VALIDATION_GATES.md.

**Trace Parser outcomes** (`evaluation/trace_parser_service.py:TraceParserService.analyze`,
`next_job`; docs/EVALUATION.md):
- **too easy** → a `world` mutation job `<t>-v<n+1>-world` (origin `mutation`) with the parent
  world, 1–2 operators from `evaluation/calibration.py:choose_mutations`, the pass review and
  the history;
- **too hard** → `revise` origin `easier` on the same world;
- **task blamed** → `revise` origin `repair_ambiguity`.

One-shot tasks still get `revise` origin `revision`. The mutation rules are deterministic,
and each choice records `{operator, rule, evidence}`. Using the world_check hardness result
in the mutation rules is parked (branch `park/hardness-tiebreak`, docs/TODO.md).

**Spec v2 / taxonomy v2** (`shared/taxonomy/taxonomy.py`, `api/submit.py`): family ×
mechanism × environment × verification, plus `shape` and `dial` 1–3. `GET /v1/families`;
`area` / `mechanism` / `environment` / `dial` on `POST /v1/tasks` and `/v1/batches`.

**Spend cap** (`shared/storage/budget.py:claim_filter`): a paid job (agent mode: world, create,
revise, rollout, analyze) is claimed only while recorded spend + the estimates of leased
jobs + its own estimate stay under `SDE_BUDGET_USD` and its batch's `budget_usd`. Agent-mode
estimates: world $1, create $4, revise $3. The batch caps live in a small `batches` table
(`id, budget_usd, created_at`). `/healthz` reports the cap, the spend and what's left.

**Cancel** (`shared/jobs/tasks.py:cancel`; `POST /v1/tasks/{id}/cancel`,
`POST /v1/batches/{id}/cancel`): status `CANCELLED`, stage `CANCELLED`, reason
`cancelled_by_user`, `cancelled_at` / `cancelled_by` / `cancel_note`. A cancel is decided
but counts as neither yield nor failure. Open jobs are closed, a running job's settle is
fenced but keeps its cost, and the queue drops the task's jobs (`TERMINAL_TASK_STATUSES`).

**Capacity and robustness** (`shared/jobs/jobqueue.py:JobQueue.claim`, `shared/storage/sql.py:claim`):
- Per-machine Docker slots (`SDE_DOCKER_SLOTS`; the job row's `needs_docker`), and agent
  slots (`SDE_AGENT_SLOTS`) for the long sessions.
- A memory guard (`SDE_MIN_FREE_MEMORY_PCT`, 20) and a CPU guard (`SDE_MAX_CPU_LOAD_PCT`, 90)
  stop new Docker claims; optional claim pacing (`SDE_DOCKER_CLAIM_SPACING_S`).
- Crash isolation: a job whose worker died (its lease expired) runs alone in a process
  (`isolated`). After `SDE_SUSPECT_MAX_WAIT_S` (600) any idle slot may take it, so it no
  longer starves on a busy machine.
- Agent jobs hold `SDE_AGENT_LEASE_TTL_S` (1800 s) leases.
- Heartbeat errors are retried, not fatal (`shared/jobs/worker.py:run_one`,
  `evaluation/executor.py:Executor._heartbeat`).
- Each process has a bounded Postgres pool (`SDE_DB_POOL_MAX`, 4), with heartbeats on
  their own connection.

**Entities and API as built** (additions to §3–§4):
- `tasks.status` can also be `CANCELLED` (reason `cancelled_by_user`, with
  `cancelled_at` / `cancelled_by` / `cancel_note`).
- `tasks.stage` gains `WORLD_BUILDING`, `WORLD_CHECKING` and `CANCELLED`.
- `tasks.current_version` is checked ≥ 1, with the cap of 8 enforced in code.
- `jobs` gains `isolated` (crash isolation) and `needs_docker` (holds a Docker slot).
- A small `batches` table (`id, budget_usd, created_at`) holds each batch's spend cap.
- New routes: `GET /v1/families`, `POST /v1/tasks/{id}/cancel`, `POST /v1/batches/{id}/cancel`;
  `area` / `mechanism` / `environment` / `dial` on task and batch creation; `budget_usd` on
  batches; the budget on `/healthz`.
- Shapes: [`API.md`](API.md).

**Limits.** `MAX_VERSIONS` = 8 (`SDE_MAX_VERSIONS`), 2 repairs per version, 3 retries per
rollout slot, 3 claims per job before DEAD.

**Settings** are listed with their defaults in README "Run it locally" and
`deploy/local.env.example`. Deployment: docs/AGENT_MODE_DEPLOY.md.

---

## 1. Functional requirements

### Core

**Operator**
1. An operator can create a task with one API call (`POST /v1/tasks`): from a free-text idea, an
   explicit spec, or nothing (the service samples one), or a batch of N with one call
   (`POST /v1/batches`, which `scripts/run_tasks.py` uses).
2. An operator can see where every candidate is (stage, version, pass pattern) at any time.
3. An operator can kill the pipeline at any point and restart it. It continues where it stopped
   and never re-runs (or re-pays for) a finished LLM call or trial.
4. An operator can run two copies at once against the same state without duplicated or corrupted
   work.
5. An operator gets a report over any set of tasks: structural / functional / learnable yield, pass
   distribution, cost, and wall time.

**System**
6. The system picks a diverse spec for each candidate (taxonomy slot + difficulty profile) and
   has GLM-5.1 author a complete Harbor task from it.
7. The system rejects or repairs drafts that fail the gates: schema checks, structural
   validator, `harbor lint`, Docker build, oracle = 1 (twice), nop = 0.
8. The system evaluates each valid version with 5 valid `terminus-2` / `glm-5.3-flash` /
   `reasoning_effort=high` rollouts. Timeouts and infrastructure errors are retried, never scored.
9. For each failed rollout, the Trace Parser records why the model failed, with evidence
   (failing tests, last commands). If a failure shows a task defect, the version goes back for a
   repair; it is not counted as hard.
10. The Trace Parser accepts 1–3/5; otherwise it sends the task back to the generation
    workers as a revision (harder or easier) up to `MAX_VERSIONS` (8, `SDE_MAX_VERSIONS`), then rejects
    it with reason `exhausted`.
11. Only accepted tasks are written to `output/accepted/<name>/`, each with its `verdict.md`.

**Reviewer (the reviewer)**
12. A reviewer can open one task and see, without reading code: its spec, every version, every
    gate result, all five rollouts with reward and evidence, the failure explanations, its cost and
    the decision.

### Below the line

- Human-likeness: blind side-by-side comparison against `examples/` on a 1–5 rubric.
- Optional Postgres / Redis / MinIO backend through `deploy/compose.dev.yaml`.
- Independent rechecks (a second 5-trial run) to measure band stability.

Difficulty tuning is **in core**: it is the main lever on yield.

### Out of scope

The three deferred items from the spec: our own inference gateway, advanced deterministic
task-mutation engines, and embedding-based novelty. Also out: K8s/Terraform manifests (the target topology is documented in §5.4), a web
dashboard, any model other than `glm-5.1` (authoring) and `glm-5.3-flash` (evaluation), and
reusing or adapting public tasks.

---

## 2. Non-functional requirements

| # | Requirement | Target | Why |
|---|---|---|---|
| N1 | **Durability of evidence** | Zero lost results once a trial or LLM call is recorded. Bundles, LLM transcripts and Harbor job dirs are immutable and never deleted; an abandoned Harbor dir is renamed, not removed. | A trial costs minutes of Docker time and real spend, and a verdict is only as good as the evidence that can be re-checked. |
| N2 | **Reliable, idempotent processing** | At-least-once execution, exactly-once *effects*: a reclaimed, retried or concurrently claimed row never produces a second scored slot, a duplicate version, a second hand-off (next-stage job), a second accepted bundle or a second terminal transition. After a restart, no finished trial or LLM call is re-run; only a call in flight at the kill can repeat, and the report says so. | A kill at any point, a restart, and two copies at once must all leave the same result. |
| N3 | **Fast acknowledgement** | `POST /v1/tasks` commits the task and its `create` job in < 1 s, before any LLM call, so a kill right after it loses nothing. `GET /v1/tasks` / `GET /v1/tasks/{id}` answer in < 1 s while workers run (WAL readers never block on writers). | The operator, including during kill tests, can see and trust state at any moment. |
| N4 | **Processing latency** | With a free slot, claimable work starts within 5 s (poll ≤ 2 s), including the next stage's job a worker just handed off. An expired lease is reclaimed within its TTL (10 min) + one poll. Pipeline overhead (everything except LLM and Harbor time) is < 5% of wall time. | Time, not money, is the constraint (§ back-of-the-envelope); the pipeline itself must not be the bottleneck. |
| N5 | **Throughput scales with slots** | Roughly linear from 1 → 2 → 4 Docker slots on one run of tasks; drafting overlaps evaluation so slots never sit idle while work exists. Measured in M5. | A 10-candidate batch is ~140 Harbor runs; slot use decides wall time. |
| N6 | **Large artifacts stay out of the database** | Bundles (≤ 2 MB, enforced by `schema.py`), Harbor job dirs (trajectories, possibly tens of MB) and transcripts are files referenced by path + sha256. DB rows stay in the KB range. | Keeps SQLite fast and the DB small enough to inspect by hand. |
| N7 | **Trustworthy verdicts** | A verdict counts only 5 *valid* rollouts of one bundle sha under one config hash; timeouts and infra errors never count as 0. Every `VALID_FAIL` cites evidence excerpts from the logs and an attribution; an explanation that cites nothing is kept and shown as `unexplained`, never hidden. Invalid rollouts are shown with their exception type. | README criterion 1: "a failing run should mean the model failed, not the task", and "know why it fails". |
| N8 | **Isolation and secrets** | No API key, tests or solution in the agent-visible image. Generated code runs only inside Harbor's containers. The key is never logged. | Reward hacking; untrusted generated Dockerfiles. |
| N9 | **Yield and diversity** | At least 40% of initial candidates accepted within the version cap and at least 80% functionally valid (working goals, reported as measured). A new task takes an unused (domain, task type, environment) combination while any is left; lexical similarity < 0.3 to any batch task or example. | Primary metric; diverse sets are worth more for training. |
| N10 | **Simplicity and cost** | Nothing beyond Docker, Python 3.12+, Harbor and `.env`: one SQLite file with two tables. Under $10 per accepted task including spend on rejected ones; hard stop at a configurable spend cap (default $100). | Anyone can run it on a laptop, and the cost per task stays far below $200. |

Consistency: every state transition, together with the next stage's job it hands off, is one
transaction (compare-and-set). Reports and `status` can be a few seconds stale.

### Back-of-the-envelope (illustrative; replaced by measurements after M2)

- Harbor runs per candidate version: oracle × 2 + nop × 1 + rollouts × 5 = **8**. Expected versions per
  candidate ≈ 1.7 (assuming about 50% need a v2 and 20% a v3; the cap of 8 versions
  raises the worst case to ≈ 64 runs per candidate), so ≈ **14 Harbor runs per candidate**.
- Docker capacity: 8 CPU / ~10 GB VM, task containers 1 CPU / 2 GB → **4 trial slots**.
- Trial time: the smoke test took 78 s on an easy example; in-band tasks will be longer. Assume
  ~8 min average → 10 candidates × 14 runs × 8 min / 4 slots ≈ **4.7 h**.
- Eval cost: ~$0.002 for the 78 s smoke trial; assume ≤ $0.05 for a long trial → ≤ $7 per
  10-candidate run. Authoring (GLM-5.1, ~30–80 k tokens per draft including repairs) is
  measured in M1. Trace parsing is one GLM-5.1 review per failed run (measured live at $0.005–0.01 each).
  Time, not money, is the constraint.

---

## 3. Core entities

Two tables, the diagram's two core entities:

- **`tasks`**: one original idea (candidate), where its current version is, and its verdict.
  The diagram's *Task*.
- **`jobs`**: one unit of work **and its result**: what it was asked to do, who ran it, when, the
  outcome, the trace and the cost. A job is created on a queue (`create`, `revise`, `validate`,
  `rollout`, `analyze`), claimed under a lease and settled once. A rollout job *is* one Harbor run of
  terminus-2: the diagram's *Job* (`jobId, taskId, status, pass/fail, traces_s3_uri,
  trace_metrics, inference_cost`). Retries are new rows, so **the jobs table is the full,
  never-overwritten history** of the run.

What we deliberately don't have:

- **No versions table.** A version is `(task_id, version)` on the jobs that built and measured it.
  Its bundle sha and origin are the output of its `create` or `revise` job, its gates are the result of its
  `validate` job, and its pass count is a count of its `rollout` jobs. Only the *current*
  version's stage needs a column (`tasks.stage`).
- **No events/audit table.** Job rows already record every step; task decisions are on `tasks`;
  LLM transcripts and traces are objects referenced from job rows. In production the job row
  inserted in the same transaction is also the outbox (`jobs.published_at`).
- **No batches table.** A batch is a nullable, indexed `tasks.batch_id` label set by
  `POST /v1/batches`; single tasks leave it null. Nothing downstream reads it: services handle
  one task's jobs, and the label only groups tasks for status and the yield report. The pinned evaluation config lives in `shared/contracts/config.py`, and every job records its
  `config_hash`.

Task bundles, LLM transcripts and raw Harbor traces are **objects** (the diagram's S3), referenced
by key and sha256, never stored in tables.

```
tasks 1───* jobs        per version: create or revise (+ repairs) · validate · rollout ×5 (+ retries)
                                     · analyze (one ping per settled rollout)

Object store (S3 in prod, ./work locally):
  bundles/<sha256>/          immutable task directories
  llm/<request_sha256>.json  prompt + response + usage, written before the job settles
  traces/<job_id>/           raw Harbor job dir(s) for one job (trajectory, verifier logs)
output/accepted/<name>/      accepted bundles + verdict.md
output/reports/<batch_id>/   summary.json (written by scripts/run_tasks.py)
```

**`tasks`**

| Column | Type | Notes |
|---|---|---|
| `id` | text PK | `t-<date>-<hex>`, or the client's `task_id`. Reusing an id with the same request inserts nothing. |
| `slot` | json | The taxonomy slot and difficulty profile it was sampled with (§6.2). |
| `status` | text | `CREATED → CALIBRATING → ACCEPTED \| REJECTED`. |
| `reason` | text, nullable | Why it was rejected: `exhausted` (out of band at `MAX_VERSIONS`), `invalid` (repairs spent), `infra_unresolved`. |
| `current_version` | int | 1–3 (check constraint). Advanced only by compare-and-set, which is what stops two processes creating the same revision. |
| `stage` | text | Stage of the current version: `QUEUED → GENERATING → BUILDING → VALIDATING → EVALUATING → BAND_OK \| OUT_OF_BAND`, or `INVALID` / `UNRESOLVED`. |
| `bundle_sha256`, `name` | text, nullable | The current version's bundle and slug (copied from its latest successful `create` / `revise` job, for cheap reads). |
| `accepted_version` | int, nullable | |
| `batch_id` | text, nullable, indexed | The batch that created it (`POST /v1/batches`); null for a single task. |
| `created_at`, `updated_at` | text (UTC ISO) | |

**`jobs`** (partial unique `(task_id, version, slot)` where `type='rollout'` and `outcome` is `VALID_PASS`/`VALID_FAIL`)

| Column | Type | Notes |
|---|---|---|
| `id` | text PK | Deterministic: `t03-v1-create`, `t03-v2-revise` (a revision), `t03-v2-repair1` (a repair, also type `revise`), `t03-v2-validate-r0`, `t03-v2-rollout3-r1`, `t03-v2-rollout3-r1-analyze` (the ping a rollout's settle enqueues). Also the Harbor `--job-name` and the `traces/<id>/` prefix. Creating a job twice is `INSERT OR IGNORE`, a no-op. |
| `task_id`, `version` | | The version this job works on. |
| `type` | text | `create` (new task from a spec) \| `revise` (repair after failed gates, or revision after the Trace Parser) \| `validate` (build, gates, oracle × 2, nop) \| `rollout` (one terminus-2 run) \| `analyze` (a rollout finished: check the version, decide when it has 5 valid rollouts). Each type is one queue: one Redis sorted set in production. |
| `slot`, `retry` | int | Rollouts: slot 1–5. An `INVALID` rollout is settled and a new row with `retry + 1` is created. |
| `input` | json | What the job was asked to do. Generate: `{origin: initial\|repair\|revision, operator?, spec, errors?}`. Rollout: the pinned agent/model/kwargs. |
| `status` | text | `READY → LEASED → DONE \| DEAD`. |
| `due_at` | text | Claimable from; also the retry backoff. The sorted-set score. |
| `priority` | int | `analyze` > `rollout` > `validate` > `revise` > `create`: finish what's started (§6.11). |
| `lease_owner`, `lease_until`, `lease_token` | | Claim, heartbeat and fencing (§6.7). |
| `attempts` | int | Times claimed. More than 1 means a lease was reclaimed; above the limit → `DEAD`. |
| `bundle_sha256`, `config_hash` | text | Exactly what was run (for generate: what was produced). |
| `outcome` | text, nullable | Rollout: `VALID_PASS \| VALID_FAIL \| INVALID` (§6.4). Generate/validate: `OK \| FAILED`. |
| `result` | json, nullable | Generate: `{name, bundle_sha256, llm_calls: [{phase, request_sha256, usage}]}`. Validate: `{gates: {schema, structural, lint, oracle: [1, 1], nop: 0}}` with log keys. Rollout: `{adopted, trial, raw_reward, task_checksum, tokens, duration_sec}` (`adopted`: a reclaimed job reused a finished Harbor result instead of rerunning). Analyze: `{action: wait \| accept \| revise \| repair \| reject, pass_count, operator?, summary}`. |
| `reward` | real, nullable | Rollout only; null when invalid. |
| `error` | text, nullable | Harbor exception type (`AgentTimeoutError`, …) or the gate/parse error summary. |
| `trace_uri` | text, nullable | `traces/<id>/`. The diagram's `traces_s3_uri`. |
| `trace_metrics` | json, nullable | Rollouts. Written by the Trace Parser's trace parser (§6.5): `{llm_calls, input_tokens, output_tokens, tool_calls: [{tool, input, output, exit_code}], files_modified, duration_seconds}` (last N tool calls). |
| `failure` | json, nullable | `VALID_FAIL` rollouts only, written by the Trace Parser: `{failing_tests, evidence: [...], explanation, attribution: model\|task\|unexplained, pitfall_matched, cited_evidence}` (§6.5). |
| `cost_usd` | real, nullable | All spend this job caused: GLM-5.1 `usage.cost` for generate and analyze (the review), Harbor `agent_result.cost_usd` for rollouts. The diagram's `inference_cost`. Batch cost = `SUM(cost_usd)`. |
| `published_at` | text, nullable | Production only: when the outbox publisher added the id to Redis. |
| `created_at`, `started_at`, `finished_at` | text | |

Status values are `text` validated by Python enums, so adding one is a code change, not a migration.

### State machines

PostgreSQL tracks the parent task lifecycle (`tasks.status`) separately from the processing
state of its current version (`tasks.stage`). A task stays `CALIBRATING` while its versions are
generated and evaluated.
Infrastructure failures and retryable errors live on **jobs** and never change a task's
difficulty score.

```
task:     CREATED ─► CALIBRATING ──► ACCEPTED       (some version BAND_OK)
                          └────────► REJECTED       reason: exhausted (OUT_OF_BAND at MAX_VERSIONS) |
                                                            invalid (repairs spent) |
                                                            infra_unresolved

stage:    QUEUED ─► GENERATING ─► BUILDING ─► VALIDATING ─► EVALUATING ─► BAND_OK | OUT_OF_BAND
                        ▲                          │                           │
                        └──────── repair (≤2) ─────┘                           └─► v(n+1) QUEUED (n < MAX_VERSIONS)
                                                   └─► INVALID       EVALUATING ─► UNRESOLVED

job:      READY ─► LEASED ─► DONE (outcome)          rollout INVALID ─► new job row, retry+1 (≤3)
                     │  ▲
                     └──┘ lease expired → reclaimed (adopt result.json if Harbor already finished)
                     └──► DEAD (attempts exhausted; a DEAD rollout still pings analyze)
```

`BUILDING` is the Docker build of the bundle's environment; `VALIDATING` covers the structural
validator, lint, oracle × 2 and nop. Both run inside one `validate` job (§6.3). Each stage
transition has one owning service (§5.1).

### How this stays extensible

| Later need | What changes |
|---|---|
| Postgres/Redis/S3 at scale | Swap the `Store` / `Queue` / `ArtifactStore` adapters (§5). Same tables in Postgres; `jobs` stays the source of truth and Redis sorted sets carry only job ids, published from rows with `published_at IS NULL`. |
| Grouping tasks for reports | A nullable `tasks.label` set by `POST /v1/tasks` and a report filter on it; no lifecycle. |
| Fine-grained timeline (every lease change, every state transition) | Add an append-only `events` table; nothing else changes. |
| Querying past versions often | Add a `task_versions` view (or table) over `jobs` grouped by `(task_id, version)`. |
| Rechecks for band stability | Rollout slots 6–10; no new table. |
| Per-requirement test tracking | Promote `coverage` from the bundle into a table. |

---

## 4. API

One FastAPI service, which is the diagram's **API Gateway** (there is no separate gateway in
front of it). JSON, under `/v1`. **`POST /v1/tasks` creates one task; `POST /v1/batches` creates N under one
`batch_id` label** so their yield can be reported together. Creation returns immediately;
nothing blocks on generation or evaluation. The operator is identified by an `X-Api-Key`
header. Errors: `404` unknown id, `409` a `task_id` or `batch_id` reused for a different
request, `422` validation.

```
POST /v1/tasks          → 202 { task_id, created }
     one of:
       { "prompt": "A Go job queue that loses jobs when a worker crashes." }
           free-text idea; it seeds the standard authoring prompt (quality rules,
           environment limits and difficulty targets still apply)
       { "spec": { "taxonomy": {...}, "difficulty": {...}, "design": ["...", ...] } }
           explicit spec; missing difficulty gets the default profile; extra keys
           are passed to the generator unchanged
       { "raw_prompt": "<the complete user message>" }
           for prompt experiments: sent to GLM-5.1 verbatim instead of the template.
           The system prompt and the response schema still apply (so does every
           gate); the task is flagged {"raw_prompt": ...} as outside the sampler
       { }
           the service samples a taxonomy slot: the least-used combination across
           every task created so far
     optional "task_id": a retry with the same id and request creates nothing
     (created: false); the same id with a different request is 409.

GET  /v1/tasks          → [{ id, name, status, reason, current_version, stage,
       ?status= &since=      pass_pattern: "✓✗✗✓✗", cost_usd, created_at }]

GET  /v1/tasks/{id}     → the verdict: { task, versions: [{ n, origin, state, bundle_sha256,
                          gates, pass_count }], rollouts: [{ id, slot, retry, outcome, reward,
                          error, trace_uri, trace_metrics, failure, cost_usd }], decision, reason }

GET  /v1/report         → summary.json (below) over a set of tasks:
       ?task_id=…&task_id=…   the tasks a client created, or
       ?since=<time>          every task created at/after a time (all tasks if neither)

POST /v1/batches        → 202 { batch_id, task_ids, created }
       { "count": 10, "batch_id"?: "...", "prompt"?: "..." | "spec"?: {...} }
           count 1–50 (default 10); tasks are <batch_id>-t01..; batch_id defaults to
           b-<date>-<hex>. One transaction: the batch exists whole or not at all.
           A prompt or spec applies to every task; with neither, each task samples
           its own slot, spread across every task so far and the batch's own picks.
           The same batch_id again with the same count and request creates nothing
           (created: false); anything else is 409.

GET  /v1/batches        → [{ batch_id, tasks, accepted, rejected, in_progress, created_at }]
                          newest first
GET  /v1/batches/{id}   → { batch_id, counts_by_status, tasks: [the /v1/tasks summary] }
GET  /v1/batches/{id}/report → summary.json (below) over the batch, plus "batch_id"

GET  /healthz           → liveness + DB/queue reachability
```

**A batch of 10** is one `POST /v1/batches`. `scripts/run_tasks.py` creates it,
polls `GET /v1/batches/{id}` until every task is final, writes the batch report to
`output/reports/<batch_id>/summary.json` and prints its yield. Rerunning the same command
resumes the same batch:

```
python scripts/run_tasks.py --count 10 [--batch run-1] [--url http://localhost:8000] [--prompt "..."]
```

The services themselves (what compose's `worker-control` / `worker-runner` run):

```
python -m api --host 0.0.0.0 --port 8000                 API service
python -m generator.service generation --with-reconciler   Generation Service (revise, then create queue) + reconciler
python -m validator.service validate                       Build + Validate
python -m evaluation.executor                            Executor (the run queue)
python -m evaluation.trace_parser_service                Trace Parser (the analyze queue)
```

Developer tools: `scripts/generate.sh "<topic>"` (one draft + gates, no evaluation),
`scripts/evaluate.sh <task_dir>` (five rollouts of an existing task directory).

`summary.json` (all counts over **initial candidates**, not versions; `null` = not measured):

```json
{
  "task_ids": ["t-20261118-1a2b3c4d", "…"], "candidates": 10,
  "structural_pass": 10, "functional_pass": 9,
  "accepted_v1": 3, "accepted_total": 6,
  "rejected": {"exhausted": 2, "invalid": 1, "infra_unresolved": 1},
  "in_progress": 0,
  "pass_count_histogram": {"0": 4, "1": 3, "2": 4, "3": 2, "4": 3, "5": 1},
  "valid_rollouts": 104, "invalid_rollouts": 7,
  "cost_usd": {"authoring": 11.2, "evaluation": 1.9, "total": 13.1, "per_accepted": 2.18},
  "wall_clock_s": 16420, "config_hash": "…",
  "yield": {"candidates": 10, "functional": 9, "accepted": 6, "accepted_v1": 3,
            "learnable_rate": 0.6}
}
```

(The numbers above are illustrative only.) `yield` is the headline number, computed the
same way for `/v1/report` and a batch report: `learnable_rate` = accepted ÷ finished
candidates (`candidates − in_progress`), `null` until one finishes.

---

## 5. High-level design

A **distributed, asynchronous job-processing system** built from **services on queues**. The API
service is the entry point; after that, every component is a service that pulls jobs from its
queue, does one stage, records the result, and **enqueues the next stage's job in the same
transaction**. Nothing orchestrates the stages from the middle. Two services own the loop:
the **Generation Service** makes tasks (new ones and revisions) and the **Trace Parser** judges
them from their agent traces. FastAPI, PostgreSQL, Redis sorted sets, independent services,
containerized execution and S3. The diagram is
[`agent-evaluation-system_calibration_v3.excalidraw`](../agent-evaluation-system_calibration_v3.excalidraw).

```
 Client ──► API service (FastAPI)   POST /v1/tasks · GET /v1/tasks · GET /v1/report
              │  one txn: tasks (CREATED) + one "create" job each
              ▼
       ┌─ create queue (sde:create) ──┐      ┌─ revise queue (sde:revise) ◄──────────────────────────┐
       ▼                             │      ▼                                                      │
 ┌──────────────────────────────────────────────────┐                                              │
 │ Generation Service  (GLM-5.1)                    │  new task from a spec, or repair / revision  │
 │ → immutable bundle in S3 → enqueue validate      │  of an existing one                          │
 └───────────────────────┬──────────────────────────┘                                              │
                         ▼ sde:validate                                                             │
 ┌──────────────────────────────────────────────────┐                                              │
 │ Build + Validate   build · lint · oracle×2 · nop │── fail: repair ─────────────────────────────►│
 └───────────────────────┬──────────────────────────┘   (≤2 per version, then REJECTED invalid)    │
                         ▼ pass: 5 jobs on the run queue (sde:rollout)                              │
 ┌──────────────────────────────────────────────────┐                                              │
 │ Executor  1 Harbor run per job → Execution Pod   │── INVALID: retry same slot (sde:rollout)      │
 │ upload whole trace to S3 → record the result     │                                              │
 └───────────────────────┬──────────────────────────┘                                              │
                         ▼ every finished run: "analyze" ping (sde:analyze)                         │
 ┌──────────────────────────────────────────────────┐                                              │
 │ Trace Parser  (analysis + revision)              │── too easy / too hard / task defect ────────►┘
 │ waits for 5 valid runs → parse traces (S3) →     │
 │ GLM-5.1 review → decide                          │┄┄ results, failure records, ACCEPTED / REJECTED ┄┄► PostgreSQL
 └──────────────────────────────────────────────────┘── accept: publish bundle + verdict.md

 S3: bundles · LLM transcripts · raw traces (write-once)
 Reconciler: reclaims expired leases, dead-letters, re-publishes, rebuilds Redis from PostgreSQL;
             a dead-lettered run still pings sde:analyze
 Analytics: read-only pass rates, cost, failure patterns, served through the API
 Inference egress gateway: FUTURE, not built; services call the SDE inference API directly
```

### 5.1 Core architecture

| Component | Queue in → what it does → queue out | Module |
|---|---|---|
| **API service (FastAPI)** | Entry point, not a worker. `POST /v1/tasks` writes one task and its `create` job in one transaction and returns `202`. Read endpoints serve status, verdicts and reports (§4). It is also the diagram's "API Gateway": no separate gateway product sits in front of it. | `api/` |
| **Generation Service** | **create queue** (`sde:create`): GLM-5.1 drafts a new task from its spec. **revise queue** (`sde:revise`): GLM-5.1 changes an existing task, either a *repair* (it failed its gates) or a *revision* (the Trace Parser found it too easy, too hard or unfair), continuing from the old task plus the evidence (§6.1, §6.6). Either way: bundle to S3, then enqueue `validate`. Revise jobs run before create jobs, so started tasks finish first. | `generator/` + `shared/jobs/worker.py` + `shared/jobs/handoffs.py` |
| **Build + Validate** | `sde:validate` → build, structural validator, `harbor lint`, oracle × 2, nop (§6.3) → **pass:** 5 jobs on the run queue; **fail:** a repair on the revise queue (≤ 2 per version), else reject the task (`invalid`). | `validator/` + `shared/jobs/worker.py` + `shared/jobs/handoffs.py` |
| **Executor** | **run queue** (`sde:rollout`) → one Harbor run in an Execution Pod, timeouts and resource limits enforced → upload the whole trace (trajectory, terminal output, verifier logs, result) to S3 → record outcome, reward, cost → **always** enqueue an `analyze` ping; an `INVALID` run also enqueues a retry of the same slot. | `evaluation/executor.py` |
| **Execution Pods** | Run each agent attempt in an isolated container (Harbor task container, 1 CPU / 2 GB). | Harbor + Docker |
| **Trace Parser** (analysis + revision) | `sde:analyze` → if the version doesn't have 5 valid runs yet, finish (`wait`). Otherwise read the 5 traces from S3, extract metrics and evidence, have GLM-5.1 review each failed run (§6.5), write `trace_metrics` and `failure` onto the run rows, and **decide** (§6.6): accept and publish; or put a revision on the revise queue; or reject. The only component that judges difficulty. Its own service; not a cron batch. | `evaluation/trace_parser_service.py` + `trace_parser.py` |
| **PostgreSQL** | Source of truth: the 2 tables in §3. Every hand-off is a row inserted in the same transaction that records the previous stage's result. | `Store` |
| **Redis sorted sets** | One queue per job type: `sde:create`, `sde:revise`, `sde:validate`, `sde:rollout` (the run queue), `sde:analyze`. Members are job ids; payload and state stay in PostgreSQL. | `Queue` |
| **Outbox publisher** | Adds committed job rows with `published_at IS NULL` to Redis. Duplicate publishes are harmless (stable ids). | `shared/jobs/jobqueue.py` |
| **S3** | Immutable task bundles, LLM transcripts, raw execution traces and logs. | `ArtifactStore` |
| **Reconciler** | Reclaims expired leases, dead-letters jobs that keep killing their worker (a dead run still pings `sde:analyze`), re-publishes unpublished jobs, rebuilds Redis from PostgreSQL. | `shared/jobs/reconciler.py` |
| **Analytics** | Read-only: pass rates, duration, cost and failure patterns by taxonomy dimension, pitfall, invalid reason and version. Serves `GET /v1/report`. | `api/` report endpoint |
| **Inference egress gateway** | **Future.** The first deployment calls the provided SDE inference API directly. | none |

**Who may change a task.** Every task change is a compare-and-set on `(current_version, stage)`,
and each stage has exactly one owner, so two writers never race on the same step:

| Stage transition | Owner |
|---|---|
| create task (`CREATED`, v1 `QUEUED`) | API service |
| `QUEUED → GENERATING → BUILDING` (bundle written) | Generation Service |
| `BUILDING → VALIDATING → EVALUATING`, or a repair back to `GENERATING`, or `INVALID → REJECTED (invalid)` | Build + Validate |
| `EVALUATING → BAND_OK → ACCEPTED`, `OUT_OF_BAND → v(n+1) QUEUED` (revision), `REJECTED (exhausted / infra_unresolved)` | Trace Parser |

### 5.2 End-to-end request flow

Client → API service → create queue → Generation Service → Build + Validate → run queue →
Executor → Execution Pods → S3 + PostgreSQL → Trace Parser → Accept, or revise queue →
Generation Service

1. **Request ingestion.** The client creates a task. The API service writes the task and its
   `create` job in **one PostgreSQL transaction**; the unpublished job rows are the outbox,
   and the outbox publisher adds them to Redis. The API returns `202` immediately.
2. **Task generation.** The Generation Service claims a `create` job, GLM-5.1 writes the task,
   the bundle is stored in S3 as an immutable, content-addressed version, and the service
   enqueues `validate`.
3. **Build + validate.** The validate worker builds the environment and runs the gates. Pass →
   it puts **five** jobs on the run queue. Fail → it puts a repair on the revise queue (≤ 2 per
   version), or rejects the task once repairs are spent.
4. **Execution.** Executors run the attempts concurrently in isolated pods. Each one uploads its
   whole trace to S3 as soon as Harbor finishes, records the result, and enqueues an `analyze`
   ping. Invalid attempts (timeouts, infra errors) are retried in the same slot, never scored.
5. **Trace parsing and calibration.** The `analyze` job that finds all five valid runs of the
   version does the work: parse the traces, a GLM-5.1 review of each failed run, then the decision.
   1–3 passes and every failure the model's fault → `ACCEPTED`, bundle published. Too easy or
   too hard → a `revise` job for v(n+1) on the revise queue, and the loop runs again from step 2.
   A failure blamed on the task → a repair revision. Still out of band at `MAX_VERSIONS` (8) →
   `REJECTED (exhausted)`.

### 5.3 Reliability and scalability

- **Services on queues:** every stage is a service on its own queue, so the API never waits on
  generation, validation, execution or trace parsing.
- **Independent scaling:** each service scales on its own queue's backlog: the Generation
  Service and the Trace Parser on model quota (GLM-5.1), Build + Validate on CPU and Docker
  builds, the Executor on sandboxes. An autoscaler (KEDA, or an AWS Auto Scaling group on a
  CloudWatch queue-depth metric) watches the queue, not HTTP traffic.
- **Hand-offs can't be lost:** a service records its result and enqueues the next job in **one
  transaction**, so a crash either loses both (the job is reclaimed and rerun) or keeps both.
- **Fault tolerance:** leases with heartbeats, retry with backoff (`due_at`), reconciliation and
  dead-lettering (`DEAD`) recover abandoned jobs (§6.7). A finished trial is uploaded to S3
  before its result is recorded, so a worker on another machine adopts it instead of rerunning.
- **Consistency:** PostgreSQL is authoritative; the transactional outbox bridges PostgreSQL and
  Redis, so a job is never in Redis without a committed row, or committed and never scheduled.
- **Idempotency:** deterministic job ids make every hand-off insert a no-op the second time;
  fencing tokens stop stale workers committing; each task stage has one owner (§5.1).
- **Execution isolation:** generated code and agent commands run only in restricted containers,
  never in trusted application processes.
- **Backpressure:** claims are capped by Docker slots per machine (validate + rollout) and the
  budget; queue priority is `analyze` > `rollout` > `validate` > `revise` > `create`, so started
  tasks finish before new ones are drafted.
- **Observability:** structured logs, job metrics, traces and the never-overwritten `jobs` history
  make every verdict auditable (§6.8).

### 5.4 Infrastructure and deployment

**Target (production).** Each component is an independently deployable workload: the API
service, the Generation Service, Build + Validate, the Executor and the Trace Parser, plus the
reconciler. PostgreSQL and Redis provide state and scheduling; S3 holds large artifacts.
Execution runs on dedicated sandbox machines, isolated from everything else. The AWS layout
(`deploy/terraform`) puts the API, Generation Service, Trace Parser and reconciler on a control
runner and Build + Validate and the Executor on N runner machines.

**Default local setup.** Same components, same code paths, but the three stores are
backed locally and all roles can run in one process, so nothing beyond Docker is needed:

| Interface | project (default) | Production |
|---|---|---|
| `Store` | SQLite file in WAL mode, `BEGIN IMMEDIATE` compare-and-set | PostgreSQL, `SELECT … FOR UPDATE` |
| `Queue` | Claimed straight from the `jobs` table with one `UPDATE … RETURNING` (§6.7); no outbox hop | Redis sorted sets fed by the outbox publisher; Lua claim + DB lease check |
| `ArtifactStore` | `./work/` content-addressed directories (write to temp, rename) | S3 |

`deploy/compose.dev.yaml` runs the production backends and every service locally.

### 5.5 How each functional requirement is met

| FR | Path through the system |
|---|---|
| 1. Create a task | API service: the task and its `create` job in one transaction. |
| 2. Status | API service → PostgreSQL (`GET /v1/tasks`, `GET /v1/tasks/{id}`). |
| 3. Kill/restart | All work is leased `jobs` rows; the reconciler reclaims expired leases; finished LLM calls and traces are found by key in S3 and reused (§6.7). |
| 4. Two copies | Every claim and commit is a compare-and-set on `lease_token`; deterministic job ids make each hand-off a no-op the second time; each task stage has one owner. |
| 5. Report | Analytics → `summary.json` + `report.md`. |
| 6. Spec + author | API service samples the slot → `create` job → Generation Service → GLM-5.1 `TaskDraft` (§6.1–6.2). |
| 7. Gates | Generation Service enqueues `validate` → Build + Validate; failures become repair jobs on the revise queue (§6.3). |
| 8. Evaluate | Build + Validate puts 5 jobs on the run queue → Executor → Execution Pods; classifier (§6.4). |
| 9. Explain failures | Executor pings `analyze` → Trace Parser: extraction + GLM-5.1 review → `trace_metrics` and `failure` on each run (§6.5). |
| 10. Accept / revise | Trace Parser → `ACCEPTED`, or a `revise` job on the revise queue (§6.6). |
| 11. Publish | Trace Parser copies the bundle + `verdict.md` to `output/accepted/<name>/` inside the ACCEPTED transition. |
| 12. Inspect | API service → `GET /v1/tasks/{id}` / `verdict.md` (§6.8). |

---

## 6. Deep dives and main code paths

### 6.1 Authoring a task (GLM-5.1)

> As built: this is **one-shot mode** (`generator/worker.py:run_generate_job`). Agent mode
> authors a world first and then a task from it (see "As built" above,
> `generator/author.py:run_world_job`, `generator/author_task.py:run_create_job`).

1. **Spec.** The sampler (§6.9) gives a taxonomy slot and difficulty profile. GLM-5.1 is first
   asked for a short *scenario pitch* (system, hidden root cause, why a capable agent slips).
   The lexical-novelty check runs on the pitch before any expensive drafting.
2. **Draft.** One call returns a `TaskDraft` (strict JSON schema from `shared/taskformat/schema.py`):
   the hidden `TaskSpec` (root cause, constraints C1…, subgoals, expected pitfalls, anti-shortcut
   measures), instruction, Dockerfile, environment files, solution, tests and the
   constraint-to-test coverage map. Pydantic validators check path safety, Dockerfile `COPY`
   sources, Python/bash syntax, test imports, coverage and instruction leaks.
3. **Materialize.** The pipeline writes `task.toml` (agent timeout 3600 s, verifier 900 s,
   1 CPU / 2 GB), `tests/test.sh`, shebangs and canary lines. Files go into a temp dir that is
   renamed to `work/bundles/<sha256>/`.
4. **Repair.** A failed check sends the exact errors plus the current files back to GLM-5.1
   ("change only what's needed, keep the constraints"), at most 2 times per version. Tests may
   not be weakened to make oracle pass: a repair that removes a test or coverage entry is rejected.

Every call is written to `work/llm/<request_sha256>.json` *before* its job settles; on restart, a matching file is reused instead of calling again (§6.7).

**What GLM-5.1 receives for each `POST /v1/tasks` input.** Every request sends the same
**system message** (`generator/prompts/system.md`: the Harbor contract and every rule the gates
check) and enforces the same **response schema** (`TaskDraft`). Only the user message differs:

| Input | User message |
|---|---|
| `{}` (sampled) or `{"spec": ...}` | the template (`prompting.render_initial_prompt`), with the taxonomy |
| `{"prompt": "..."}` | the same template, with the text as its TASK IDEA section |
| `{"raw_prompt": "..."}` | the text verbatim; no template |

### 6.2 Difficulty profile (from `TASK_GENERATION_RULES.md`)

> As built: spec v2 (family, mechanism, environment, verification, shape, dial 1–3;
> `shared/taxonomy/taxonomy.py`) drives agent mode; this v1 profile still drives one-shot drafts.

Each spec carries **both** kinds of levers; difficulty is never reduced to counts alone.

**Quantitative levers** (code can check them in the bundle):

| Lever | Example | Checked by |
|---|---|---|
| Input size | 10 vs. 100 relevant files; 10 k-row fixture | counting files/rows after materialize |
| Component count | 1 module vs. 4 interacting modules | modules touched by `solve.sh` |
| Constraint count | correctness only vs. + ordering + idempotency | `spec.correctness_constraints` + coverage |
| Required scenarios | normal run vs. + crash recovery | tests per scenario |
| Dependency depth | 1 vs. 3 dependent subgoals | `spec.subgoals` |

**Semantic levers** (the prompt controls them):

| Lever | Easier | Harder |
|---|---|---|
| Diagnosis difficulty | Bug named in the instruction | Agent must find the root cause |
| Constraint interaction | Independent requirements | Fixing one can break another |
| Information discovery | Relevant logs handed over | Agent decides which evidence matters |
| Failure complexity | Happy path | Concurrency, partial failure, recovery |
| Planning complexity | One obvious step | Several dependent decisions |

Rules carried over:

- **Constraint interaction and diagnosis difficulty matter most.** Two subtle requirements that
  conflict beat twenty independent ones.
- **Decision points are a hypothesis.** The count in the spec is a target. Whether the agent
  actually had to make those decisions is checked afterwards in failing trajectories (§6.5).
  If failures never touch the intended decision, that is recorded.
- **Test count is a quality control, not a difficulty knob.** We never add tests to push the pass
  rate down, never hide necessary information, and never add requirements that can't be met.
- Input size must be relevant code/data, not filler.

**Default (2026-11-18): a task form and a dial, not generic counts.** The pilots measured
that easy/medium/hard labels and file, constraint or subgoal counts don't move the pass rate
(every level scored 4–5/5), so `spec.difficulty` is now `{"form": ..., "dial": 1–3, "notes"?}`
(generator/prompting.py `FORMS`). The default is `{"form": "behavior_preserving", "dial": 2}`:
a stated local change to a shared component that silently breaks dependents relying on its old
behavior. It is the only form that produced a genuine model failure in the pilots (4/5), so it is
the best starting point, not a proven band-hitter: the dial turns it. `many_rules` (many precise
interacting rules) is the second form. Free-text prompts get a form and dial too; without one,
GLM-5.1 falls back to "a tool with planted 1–2 line bugs", which flash solves 4–5/5. The
system prompt carries every rule plus BAD/GOOD examples of the mistakes earlier drafts made.

### 6.3 Validation gates (in order, cheapest first)

> As built: `wrong_fix` runs after nop, `leak` is a semantic review plus a blind fix
> prediction that also runs inside `create`, and agent mode adds `world_check`
> ([`VALIDATION_GATES.md`](VALIDATION_GATES.md)).

| # | Gate | Pass condition | On failure |
|---|---|---|---|
| 1 | `TaskDraft` validators | no errors | repair |
| 2 | `validator/validate.py` | exit 0 | repair |
| 3 | `harbor lint --json` | no `agent-image-test-leak`, `unpinned-image`, `unpinned-python-dependency`, `root-agent` | repair; other findings are warnings |
| 4 | oracle trial ×2 | reward 1 both times | repair (flaky = fail) |
| 5 | nop trial | reward exactly 0, no verifier exception | repair |
| 6 | leak review (one GLM-5.1 call, ~$0.02–0.05) | the instruction doesn't give away the hidden root cause, subgoals or constraints; a leak counts only if its quote appears verbatim in the instruction | repair: reword at the interface level |

A verifier crash or missing reward is **not** nop = 0. Docker build failures show up in gates 4–5
as Harbor environment exceptions and are repaired with the build log. The leak review runs last,
so it is paid only for tasks that already work, and before any rollout is spent on a task whose
instruction hands over the fix; its verdict is `result.gates.leak` and its cost the job's
`cost_usd` (`SDE_LEAK_REVIEW=0` disables it; bundles with no hidden spec skip it). Gates 2–5 run inside one
`validate` job, which holds one Docker slot; its Harbor dirs go under `traces/<job_id>/oracle1`,
`…/oracle2` and `…/nop`, so a reclaimed validate job adopts finished steps instead of rerunning
them. Each step's result is recorded in the validate job's `result.gates`.

**The repair loop runs through the revise queue, not inside Build + Validate.** A failed gate
puts a repair on the revise queue with the gate's evidence (`feedback`: the failing gate, the
failing test and its assertion, the tail of the build log or of `solve.sh`'s output). The
Generation Service continues GLM-5.1's own authoring conversation with it, and the new bundle
comes back to validate. At most 2 repairs per version, then `REJECTED (invalid)`. Looping inside
the validate worker would hold a Docker slot idle through each 4–12 min GLM-5.1 call, mix the
model client into the Docker-bound service, and lose per-step crash safety; the queue hop
itself costs one row. The cap is measured: in task-builder's pilots (53 candidates), 38 needed no
repair, 11 one and 4 two, and 2 of those 4 still failed. The first repair rescues most drafts
(hard briefs: 3/8 pass first time, 6/8 with repairs), and drafts that survive two rarely
converge, as when a test can't pass in the environment at all.

### 6.4 Evaluation and trial classification

One `harbor run -p <bundle> -a terminus-2 -m openrouter/z-ai/glm-5.3-flash --ak
reasoning_effort=high --n-attempts 1 --job-name <job_id> -o work/traces/<job_id>` per `rollout`
job. The classifier reads the trial's `result.json` (Harbor 0.26.0 `TrialResult`):

| Result | Rule |
|---|---|
| `INVALID` | `exception_info` is set (`AgentTimeoutError`, `AgentSetupTimeoutError`, `EnvironmentStartTimeoutError`, `VerifierTimeoutError`, `RewardFileNotFoundError`, `RewardFileEmptyError`, `VerifierOutputParseError`, API/LiteLLM errors, …), or no `verifier_result`, or a reward other than 0/1, or the trial's `task_checksum` / config doesn't match the version. |
| `VALID_PASS` | No exception, reward = 1. |
| `VALID_FAIL` | No exception, reward = 0. |

Every settled rollout enqueues an `analyze` ping in the same transaction. An `INVALID` rollout
also enqueues a new `rollout` job (same slot, retry + 1), at most 3 retries per slot. When a
slot has used its retries without a valid result, the Trace Parser sets the version
`UNRESOLVED` and the task `REJECTED (infra_unresolved)`, never scored as hard. If invalid rollouts pile up
across tasks (e.g. gateway outage: more than 5 in 10 minutes), the Executor pauses.

### 6.5 Why each failure is the model's fault (Trace Parser)

The Trace Parser runs this once per version, when its five valid rollouts are in, reading
the traces the executors uploaded to S3. For every rollout it extracts `trace_metrics` with code
(LLM calls, tokens, last tool calls, files the agent wrote, duration). For every `VALID_FAIL` it
builds an **evidence record** with code first:

- failing test IDs and assertion messages from the verifier output;
- the agent's last N commands and its final message from the trajectory;
- which files the agent changed, compared with the files `solve.sh` changes.

Then GLM-5.1 (authoring model, allowed for diagnosis; no new model) reviews each failed run, one
call per failure, with its evidence, the task instruction and the hidden `TaskSpec`. It returns
`{explanation, attribution: model|task, pitfall_matched, cited_evidence}`. It must cite evidence
ids; an explanation that cites nothing real, or a review that can't run (GLM-5.1 aborts
strict-JSON output fairly often, so each review is tried up to 3 times), is recorded as
`unexplained`: kept, never trusted. The call is cached per run, so a reclaimed analyze job never
pays twice.

- `attribution = task` (ambiguous instruction, environment bug, test checking something not
  asked for) → the version is `INVALID` and goes to a `repair_ambiguity` revision. It doesn't
  count as a valid difficulty measurement.
- Accepted tasks must have **every** failure attributed to the model, with evidence. That is the
  "know why it fails" requirement.
- `pitfall_matched` checks whether failures hit the intended decision points (§6.2) or something
  incidental.

### 6.6 Aggregation and revision (Trace Parser)

> As built: too easy in agent mode mutates the **world** (a `world` job with
> evidence-chosen operators), too hard is `revise` origin `easier`, and a task-blamed
> failure is `repair_ambiguity` ([`EVALUATION.md`](EVALUATION.md)).

When a version has 5 valid rollout jobs under one bundle sha and one config hash, the Trace
Parser decides (an `analyze` job that finds fewer finishes with `action: wait`):

```
pass_count in 1..3, all failures model → BAND_OK, task ACCEPTED, publish
any failure attribution task      → repair revision (repair_ambiguity), not a difficulty measurement
pass_count 0 or 4..5, n < MAX      → OUT_OF_BAND, advance to v(n+1) and enqueue its revision job
pass_count 0 or 4..5, n = MAX      → OUT_OF_BAND, task REJECTED (exhausted)   (MAX = MAX_VERSIONS, 8)
```

Moving to v(n+1) is one compare-and-set, `UPDATE tasks SET current_version=n+1, stage='QUEUED'
WHERE id=? AND current_version=n AND stage='EVALUATING'`, in the same transaction as inserting the
`revise` job with its deterministic id, so two analyze jobs that both see five valid rollouts
can't both create it: one wins, the other finds the stage already moved and finishes.

**Version cap: `MAX_VERSIONS` = 8** (`SDE_MAX_VERSIONS`), raised from 3 on 2026-11-18 (briefly 10, then 8). Each extra
version costs about $0.10–0.30 of drafting, $0.03–0.07 of runs and 20–40 minutes, so a task that
never reaches the band costs up to ~8× a single evaluation. The cap lives in code, not in the
table (`tasks.current_version` is only checked `>= 1`; schema init lifts the old 3-version check).
Once every operator in a direction has been used, the first one is applied again on top of the
previous version.

Operators (fixed rules, chosen from the evidence):

| Observed | Operator | What GLM-5.1 is asked to change |
|---|---|---|
| 4–5/5, passes are short / one-file patches | `increase_constraint_interaction` | add a constraint that the obvious fix breaks |
| 4–5/5, root cause found immediately | `increase_diagnosis_depth` | remove redundant hints; show symptoms instead of the cause |
| 4–5/5, single-component fix | `increase_cross_component_dependency` | make the fix span two modules |
| 0/5, failures spread over unrelated constraints | `reduce_unrelated_constraints` | drop the constraint no trial got close to |
| 0/5, agents never find the right subsystem | `localize_diagnosis` | point at the subsystem, not the bug |

A revision returns a full new `TaskDraft` plus a change summary. It goes through all gates and
5 new rollouts. Earlier bundles are never modified.

### 6.7 Claims, leases, resume and two copies

All coordination is in SQLite (WAL, `busy_timeout` 30 s). A claim is one `BEGIN IMMEDIATE`
transaction:

```sql
-- claim the next job of a type; validate and rollout jobs also need a free Docker slot
-- (generate and analyze only call the model, so they don't)
UPDATE jobs SET status='LEASED', lease_owner=:me, lease_until=:now+600,
       lease_token=lease_token+1, attempts=attempts+1, started_at=COALESCE(started_at,:now)
 WHERE id = (SELECT id FROM jobs
              WHERE type=:type AND due_at <= :now
                AND (status='READY' OR (status='LEASED' AND lease_until < :now))
              ORDER BY priority DESC, due_at LIMIT 1)
   AND (:type IN ('generate', 'analyze') OR
        (SELECT COUNT(*) FROM jobs WHERE type IN ('validate','rollout')
           AND status='LEASED' AND lease_until >= :now) < :slots)
RETURNING id, lease_token;
```

- **Heartbeat** every 60 s extends `lease_until`, and only if `lease_token` is still ours.
- **Commit** of any result is `UPDATE … WHERE id=? AND lease_token=?`, in the same transaction
  as the next stage's job (the hand-off). A worker whose lease was taken over (e.g. it was
  paused) commits nothing, so it hands off nothing either (fencing).
- **Reclaiming a job:** before starting Harbor again, the new owner fetches the job's trace from
  S3 (executors upload it the moment Harbor finishes) and looks in `work/traces/<job_id>/`. If a
  finished `result.json` is there, it **adopts** it instead of rerunning, which closes the
  "killed after Harbor finished, before commit" gap, even across machines. A half-finished
  dir is renamed to `<job_id>.abandoned-<n>` and kept for the audit.
- **LLM calls:** the request is hashed. If `work/llm/<sha>.json` exists, the response is reused.
  A kill during the HTTP call is the only case that pays twice; the job's `attempts > 1`
  shows it, and the report counts these.
- **Harbor collisions:** Harbor job names and dirs are the (unique) job id. Bundle dirs are
  content-addressed, so two processes writing the same bundle write identical bytes (write to
  temp, rename, ignore "exists").
- **Publishing** to `output/accepted/` happens inside the Trace Parser's `ACCEPTED`
  transition: copy to temp, rename. A second analyze job sees `ACCEPTED` and skips.

Tests (§8, M3): kill `-9` at random points over 20 runs, check no rollout ran twice (Harbor
dirs = rollout job rows) and the final counts are unchanged; run two copies of every service on the same tasks
and check the uniqueness invariants.

### 6.8 Audit records

Per task, `GET /v1/tasks/{id}` and `verdict.md` (also written into the accepted bundle) show:

1. **Decision** with its rule: "ACCEPTED: v2 passed 2/5 valid rollouts (band 1–3)".
2. **Spec**: taxonomy slot, difficulty profile, hidden `TaskSpec` (root cause, constraints,
   intended pitfalls).
3. **Versions**: per version, origin/operator, bundle sha, gate table with log paths, pass
   pattern.
4. **Rollouts**: table of slot, retry, outcome, reward, duration, cost, trace; invalid retries
   shown with their exception type.
5. **Failures**: per failed rollout, the evidence excerpts, explanation and attribution, as
   written by the Trace Parser; plus the analyze job's summary of why the version was
   accepted, revised or rejected.
6. **Cost and time**: LLM vs. eval, wall time per stage.

Everything links to raw files (`work/llm/…`, `work/traces/…`), so a reviewer can check any claim
against the source. The task's job rows, ordered by time, are the full timeline
(`GET /v1/tasks/{id}` lists every rollout; the `jobs` rows hold the rest).

### 6.9 Diversity and novelty

Taxonomy (from `TASK_GENERATION_RULES.md`), kept in `config/taxonomy.yaml`:

| Dimension | Example values |
|---|---|
| Domain | Distributed systems, databases, networking, security, ML infrastructure, data pipelines, Linux tooling |
| Task type | Debugging, implementation, migration, optimization, investigation |
| Core capability | Concurrency, consistency, resource management, performance |
| Failure mode | Race condition, crash, timeout, resource exhaustion, data corruption |
| Environment | Python service, Java application, Go/C binary, Linux container, database (SQLite/Postgres in-container) |
| Skill interaction | Single skill, two dependent skills, multiple interacting constraints |

- **Quota sampler:** each new candidate takes the least-used value on each dimension (with
  seeded tie-breaking), skipping combinations listed as incompatible. Results are stored in
  `tasks.slot`.
- **Spread:** a new task takes an unused (domain, task type, environment) combination across
  every task so far; repeats only once all have been used.
- **Lexical novelty:** word 5-gram MinHash Jaccard of instruction + `TaskSpec.root_cause` against
  every earlier bundle and the six `examples/`. Above 0.3 → new pitch. This is a heuristic, not
  semantic novelty (that's the deferred embedding feature).
- **Report:** distribution of every dimension across the reported tasks.

### 6.10 Task quality and reward hacking

- **Behavioral tests:** tests drive the system through its interface (CLI, HTTP, files) and
  check observable results, not the reference implementation's internals. At least one test
  uses inputs generated at verify time (seeded randomness in the test), so hard-coded outputs
  fail.
- **Hidden grading:** tests and solution are never in the build context (checked by `schema.py`
  and lint). Harbor uploads `/tests` only at verification. The instruction never mentions them.
- **Verifier isolation (open, D2):** pytest runs in an interpreter the agent can't have modified.
- **Sensitivity:** nop must fail; a wrong-but-plausible solution must fail (open, D3).
- **Instruction quality:** every path, format and command the tests rely on must appear in the
  instruction (the coverage map makes this checkable). No hidden requirements.
- **Training value:** pitfalls must be engineering mistakes a better model learns to avoid
  (missed edge case, unhandled concurrency), not trivia or guessing.

### 6.11 Cost and throughput

- Cost from the source: `usage.cost` on each GLM response and `agent_result.cost_usd` from
  Harbor, both summed into the `cost_usd` of the job that caused them (each GLM call is also listed
  in the create / revise job's `result.llm_calls`). Missing values are recorded as
  null, not estimated.
- A budget check runs before every paid action: stop claiming new work at the spend cap.
- Scheduling favors finishing what's started: analyze before rollouts, rollouts before new
  validations, validations before revisions, and revisions before new drafts (`priority`:
  the revise queue drains before the create queue). A new candidate is only drafted when fewer
  than 2 versions are waiting for Docker slots.

### 6.12 Security

- `.env` holds the key; it is passed to Harbor's host process only. Task containers never get it
  (terminus-2 runs the model on the host).
- Generated code runs only inside Harbor containers. The pipeline never executes generated
  scripts on the host, except `bash -n` and Python `compile()` for syntax checks.
- Logs and transcripts never include the key (requests are logged without headers).

---

## 7. Open decisions

| # | Decision | Proposal |
|---|---|---|
| D1 | Simplify the spec's data model (10 tables, Redis outbox) | **Adopt this PRD's 2 tables** (`tasks`, `jobs`): the job row is the queue entry, the rollout record, the audit history and the outbox; versions are `(task_id, version)` on jobs; tasks are created one per request or N per batch, a batch being a `tasks.batch_id` label (2026-11-18). **Decided 2026-11-17:** workers on queues hand off directly (§5); no central orchestrator; the Trace Parser owns accept / revise / reject. Same guarantees (leases, fencing, never-overwritten history, idempotent ids). Update the spec's §4–5 to point here. |
| D2 | How `tests/test.sh` gets pytest | Install pinned `uv` + pytest at verify time into a fresh interpreter (agent can't tamper). Accept the `verifier-runtime-install` / `verifier-network-download` lint warnings and document why. Alternative: pytest baked into the image, lint-clean but tamperable. |
| D3 | Wrong-solution gate | Add `wrong_solution_script` to `TaskDraft` (the pitfall the spec predicts, as code); must score 0. Cheap and the strongest test-sensitivity evidence. |
| D4 | Diagnosis prompt | Use GLM-5.1 to explain failures, bound to code-extracted evidence (§6.5). The alternative, code-only evidence with no prose, is weaker for "know why it fails". |
| D5 | Where `sde` lives | New `pipeline/` package; `generator/` keeps `schema.py`, prompts and `generate.py`; `validator/` unchanged. |

## 8. Milestone plan

1. **M0 housekeeping:** commit the spec, diagram, `schema.py`, this PRD and the `config.py` change;
   `pyproject.toml` / `.env.example`.
2. **M1 one valid task:** client, sampler, prompt, materialize, gates 1–5 with repairs.
   `sde generate` produces a task with oracle = 1 and nop = 0. Hand-review it against `examples/`.
3. **M2 calibration:** executor workers (trial runner, classifier, trace upload, retries). Pilot
   3–5 candidates and tune the v1 difficulty default from what `glm-5.3-flash` actually does.
4. **M3 robustness:** SQLite store (2 tables), job leases, adoption, the service entry points, kill and
   two-copy tests. (Built before the real run so that run uses it.)
5. **M3b services on queues:** the Generation Service (create + revise queues), Build + Validate
   and the Executor each hand off their next stage in their settle transaction; executors ping
   `sde:analyze`; the Trace Parser service extracts evidence and has GLM-5.1 review each failed run, writes `failure` / `trace_metrics`, and decides accept /
   revision / repair / reject. Owner per stage transition as in §5.1.
6. **M4 revision loop:** operators, v2…v8 through the Generation Service's revision path, before/after in the report.
7. **M5 run:** 10+ candidates via `scripts/run_tasks.py` (one batch), its batch report, update `CHANGELOG.md` with measured results.
8. **M6 human-likeness:** blind rubric comparison with `examples/`; optional compose backend.
