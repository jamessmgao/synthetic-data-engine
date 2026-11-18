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
every failure. Tasks outside the band are revised up to v3, then rejected. Everything is recorded so a reviewer
can check each verdict without reading code, and the pipeline survives being killed or run twice.

---

## 1. Functional requirements

### Core

**Operator**
1. An operator can start a batch of N new candidate tasks with one command (`sde run --count 10`).
2. An operator can see where every candidate is (stage, version, pass pattern) at any time.
3. An operator can kill the pipeline at any point and restart it. It continues where it stopped
   and never re-runs (or re-pays for) a finished LLM call or trial.
4. An operator can run two copies at once against the same state without duplicated or corrupted
   work.
5. An operator gets a batch report: structural / functional / learnable yield, pass
   distribution, cost, and wall time.

**System**
6. The system picks a diverse spec for each candidate (taxonomy slot + difficulty profile) and
   has GLM-5.1 author a complete Harbor task from it.
7. The system rejects or repairs drafts that fail the gates: schema checks, structural
   validator, `harbor lint`, Docker build, oracle = 1 (twice), nop = 0.
8. The system evaluates each valid version with 5 valid `terminus-2` / `glm-5.3-flash` /
   `reasoning_effort=high` rollouts. Timeouts and infrastructure errors are retried, never scored.
9. For each failed rollout, the Analysis service records why the model failed, with evidence
   (failing tests, last commands). If a failure shows a task defect, the version goes back for a
   repair; it is not counted as hard.
10. The Analysis service accepts 1–3/5; otherwise it sends the task back to the generation
    workers as a revision (harder or easier) up to v3, then rejects it with reason `exhausted`.
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
| N3 | **Fast acknowledgement** | `sde run --count N` commits the tasks and their `generate` jobs in < 1 s, before any LLM call, so a kill right after it loses nothing. `sde status` / `sde inspect` answer in < 1 s while workers run (WAL readers never block on writers). | The operator, including during kill tests, can see and trust state at any moment. |
| N4 | **Processing latency** | With a free slot, claimable work starts within 5 s (poll ≤ 2 s), including the next stage's job a worker just handed off. An expired lease is reclaimed within its TTL (10 min) + one poll. Pipeline overhead (everything except LLM and Harbor time) is < 5% of wall time. | Time, not money, is the constraint (§ back-of-the-envelope); the pipeline itself must not be the bottleneck. |
| N5 | **Throughput scales with slots** | Roughly linear from 1 → 2 → 4 Docker slots on one batch; drafting overlaps evaluation so slots never sit idle while work exists. Measured in M5. | A 10-candidate batch is ~140 Harbor runs; slot use decides wall time. |
| N6 | **Large artifacts stay out of the database** | Bundles (≤ 2 MB, enforced by `schema.py`), Harbor job dirs (trajectories, possibly tens of MB) and transcripts are files referenced by path + sha256. DB rows stay in the KB range. | Keeps SQLite fast and the DB small enough to inspect by hand. |
| N7 | **Trustworthy verdicts** | A verdict counts only 5 *valid* rollouts of one bundle sha under one config hash; timeouts and infra errors never count as 0. Every `VALID_FAIL` cites evidence excerpts from the logs and an attribution; an explanation that cites nothing is kept and shown as `unexplained`, never hidden. Invalid rollouts are shown with their exception type. | README criterion 1: "a failing run should mean the model failed, not the task", and "know why it fails". |
| N8 | **Isolation and secrets** | No API key, tests or solution in the agent-visible image. Generated code runs only inside Harbor's containers. The key is never logged. | Reward hacking; untrusted generated Dockerfiles. |
| N9 | **Yield and diversity** | At least 40% of initial candidates accepted within v3 and at least 80% functionally valid (working goals, reported as measured). No two tasks in a batch share (domain, task type, environment); lexical similarity < 0.3 to any batch task or example. | Primary metric; diverse sets are worth more for training. |
| N10 | **Simplicity and cost** | Nothing beyond Docker, Python 3.12+, Harbor and `.env`: one SQLite file with two tables, one `sde` binary. Under $10 per accepted task including spend on rejected ones; hard stop at a configurable batch cap (default $100). | Anyone can run it on a laptop, and the cost per task stays far below $200. |

Consistency: every state transition, together with the next stage's job it hands off, is one
transaction (compare-and-set). Reports and `status` can be a few seconds stale.

### Back-of-the-envelope (illustrative; replaced by measurements after M2)

- Harbor runs per candidate version: oracle × 2 + nop × 1 + rollouts × 5 = **8**. Expected versions per
  candidate ≈ 1.7 (assuming about 50% need v2 and 20% need v3), so ≈ **14 Harbor runs per candidate**.
- Docker capacity: 8 CPU / ~10 GB VM, task containers 1 CPU / 2 GB → **4 trial slots**.
- Trial time: the smoke test took 78 s on an easy example; in-band tasks will be longer. Assume
  ~8 min average → 10 candidates × 14 runs × 8 min / 4 slots ≈ **4.7 h**.
- Eval cost: ~$0.002 for the 78 s smoke trial; assume ≤ $0.05 for a long trial → ≤ $7 per
  10-candidate batch. Authoring (GLM-5.1, ~30–80 k tokens per draft including repairs) is
  measured in M1. Analysis is one GLM-5.1 review per version (measured live at $0.005–0.01).
  Time, not money, is the constraint.

---

## 3. Core entities

Two tables, the diagram's two core entities:

- **`tasks`**: one original idea (candidate), where its current version is, and its verdict.
  The diagram's *Task*.
- **`jobs`**: one unit of work **and its result**: what it was asked to do, who ran it, when, the
  outcome, the trace and the cost. A job is created on a queue (`generate`, `validate`,
  `rollout`, `analyze`), claimed under a lease and settled once. A rollout job *is* one Harbor run of
  terminus-2: the diagram's *Job* (`jobId, taskId, status, pass/fail, traces_s3_uri,
  trace_metrics, inference_cost`). Retries are new rows, so **the jobs table is the full,
  never-overwritten history** of the run.

What we deliberately don't have:

- **No versions table.** A version is `(task_id, version)` on the jobs that built and measured it.
  Its bundle sha and origin are the output of its `generate` job, its gates are the result of its
  `validate` job, and its pass count is a count of its `rollout` jobs. Only the *current*
  version's stage needs a column (`tasks.stage`).
- **No events/audit table.** Job rows already record every step; task decisions are on `tasks`;
  LLM transcripts and traces are objects referenced from job rows. In production the job row
  inserted in the same transaction is also the outbox (`jobs.published_at`).
- **No batches table.** A batch is the `tasks.batch_id` label on the tasks one submission
  created. The pinned evaluation config lives in `shared/contracts/config.py`, and every job records its
  `config_hash`.

Task bundles, LLM transcripts and raw Harbor traces are **objects** (the diagram's S3), referenced
by key and sha256, never stored in tables.

```
tasks 1───* jobs        per version: generate (+ repairs) · validate · rollout ×5 (+ retries)
                                     · analyze (one ping per settled rollout)

Object store (S3 in prod, ./work locally):
  bundles/<sha256>/          immutable task directories
  llm/<request_sha256>.json  prompt + response + usage, written before the job settles
  traces/<job_id>/           raw Harbor job dir(s) for one job (trajectory, verifier logs)
output/accepted/<name>/      accepted bundles + verdict.md
output/reports/<batch_id>/   summary.json + report.md
```

**`tasks`**

| Column | Type | Notes |
|---|---|---|
| `id` | text PK | `<batch_id>-t03`. Deterministic, so resubmitting the same batch inserts nothing new (`INSERT OR IGNORE`). |
| `batch_id` | text | Label, e.g. `b-20261117-01`. Indexed. |
| `slot` | json | The taxonomy slot and difficulty profile it was sampled with (§6.2). |
| `status` | text | `CREATED → CALIBRATING → ACCEPTED \| REJECTED`. |
| `reason` | text, nullable | Why it was rejected: `exhausted` (out of band at v3), `invalid` (repairs spent), `infra_unresolved`. |
| `current_version` | int | 1–3 (check constraint). Advanced only by compare-and-set, which is what stops two processes creating the same revision. |
| `stage` | text | Stage of the current version: `QUEUED → GENERATING → BUILDING → VALIDATING → EVALUATING → BAND_OK \| OUT_OF_BAND`, or `INVALID` / `UNRESOLVED`. |
| `bundle_sha256`, `name` | text, nullable | The current version's bundle and slug (copied from its latest successful `generate` job, for cheap reads). |
| `accepted_version` | int, nullable | |
| `created_at`, `updated_at` | text (UTC ISO) | |

**`jobs`** (partial unique `(task_id, version, slot)` where `type='rollout'` and `outcome` is `VALID_PASS`/`VALID_FAIL`)

| Column | Type | Notes |
|---|---|---|
| `id` | text PK | Deterministic: `t03-v2-generate`, `t03-v2-repair1`, `t03-v2-validate-r0`, `t03-v2-rollout3-r1`, `t03-v2-rollout3-r1-analyze` (the ping a rollout's settle enqueues). Also the Harbor `--job-name` and the `traces/<id>/` prefix. Creating a job twice is `INSERT OR IGNORE`, a no-op. |
| `task_id`, `version` | | The version this job works on. |
| `type` | text | `generate` (draft, repair or revision) \| `validate` (build, gates, oracle × 2, nop) \| `rollout` (one terminus-2 run) \| `analyze` (a rollout finished: check the version, decide when it has 5 valid rollouts). Each type is one queue: one Redis sorted set in production. |
| `slot`, `retry` | int | Rollouts: slot 1–5. An `INVALID` rollout is settled and a new row with `retry + 1` is created. |
| `input` | json | What the job was asked to do. Generate: `{origin: initial\|repair\|revision, operator?, spec, errors?}`. Rollout: the pinned agent/model/kwargs. |
| `status` | text | `READY → LEASED → DONE \| DEAD`. |
| `due_at` | text | Claimable from; also the retry backoff. The sorted-set score. |
| `priority` | int | `analyze` > `rollout` > `validate` > `generate`: finish what's started (§6.11). |
| `lease_owner`, `lease_until`, `lease_token` | | Claim, heartbeat and fencing (§6.7). |
| `attempts` | int | Times claimed. More than 1 means a lease was reclaimed; above the limit → `DEAD`. |
| `bundle_sha256`, `config_hash` | text | Exactly what was run (for generate: what was produced). |
| `outcome` | text, nullable | Rollout: `VALID_PASS \| VALID_FAIL \| INVALID` (§6.4). Generate/validate: `OK \| FAILED`. |
| `result` | json, nullable | Generate: `{name, bundle_sha256, llm_calls: [{phase, request_sha256, usage}]}`. Validate: `{gates: {schema, structural, lint, oracle: [1, 1], nop: 0}}` with log keys. Rollout: `{adopted, trial, raw_reward, task_checksum, tokens, duration_sec}` (`adopted`: a reclaimed job reused a finished Harbor result instead of rerunning). Analyze: `{action: wait \| accept \| revise \| repair \| reject, pass_count, operator?, summary}`. |
| `reward` | real, nullable | Rollout only; null when invalid. |
| `error` | text, nullable | Harbor exception type (`AgentTimeoutError`, …) or the gate/parse error summary. |
| `trace_uri` | text, nullable | `traces/<id>/`. The diagram's `traces_s3_uri`. |
| `trace_metrics` | json, nullable | Rollouts. Written by the Analysis service's trace parser (§6.5): `{llm_calls, input_tokens, output_tokens, tool_calls: [{tool, input, output, exit_code}], files_modified, duration_seconds}` (last N tool calls). |
| `failure` | json, nullable | `VALID_FAIL` rollouts only, written by the Analysis service: `{failing_tests, evidence: [...], explanation, attribution: model\|task\|unexplained, pitfall_matched, cited_evidence}` (§6.5). |
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
                          └────────► REJECTED       reason: exhausted (v3 OUT_OF_BAND) |
                                                            invalid (repairs spent) |
                                                            infra_unresolved

stage:    QUEUED ─► GENERATING ─► BUILDING ─► VALIDATING ─► EVALUATING ─► BAND_OK | OUT_OF_BAND
                        ▲                          │                           │
                        └──────── repair (≤2) ─────┘                           └─► v(n+1) QUEUED (n<3)
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
| Batch-level settings or quotas | Add a `batches` table then; `tasks.batch_id` is already the foreign key. |
| Fine-grained timeline (every lease change, every state transition) | Add an append-only `events` table; nothing else changes. |
| Querying past versions often | Add a `task_versions` view (or table) over `jobs` grouped by `(task_id, version)`. |
| Rechecks for band stability | Rollout slots 6–10; no new table. |
| Per-requirement test tracking | Promote `coverage` from the bundle into a table. |

---

## 4. API

One FastAPI service, which is the diagram's **API Gateway** (there is no separate gateway in
front of it). JSON, under `/v1`. Submission returns immediately; nothing blocks on generation or
evaluation. The operator is identified by an `X-Api-Key` header. Errors: `404` unknown id, `409`
conflicting batch id with a different count, `422` validation.

```
POST /v1/batches                      → 202 { batch_id, task_ids }
     { count: 10, batch_id? }           Idempotent on batch_id: task ids are <batch_id>-t01…tNN,
                                        so a repeat creates nothing new.

GET  /v1/batches/{batch_id}           → { batch_id, counts_by_status,
                                          tasks: [{ id, status, current_version, version_state,
                                                    pass_pattern: "✓✗✗✓✗", cost_usd }] }

GET  /v1/tasks/{task_id}              → the verdict: { task, versions: [{ n, origin, state,
                                          bundle_sha256, gates, pass_count }],
                                          rollouts: [{ id, slot, retry, outcome, reward, error,
                                          trace_uri, trace_metrics, failure, cost_usd }],
                                          decision, reason }

GET  /v1/batches/{batch_id}/report    → summary.json (below), from the Analytics service
GET  /healthz                         → liveness + DB/queue reachability
```

The `sde` CLI calls the same service functions directly, so it works with or without the server
running:

```
sde run --count 10 [--batch ID]   submit (POST /v1/batches) + run every worker role until done
sde work [--roles …]              run worker roles only: generate, validate, rollout, analyze, reconcile
sde status <batch>                GET /v1/batches/{id}
sde inspect <task>                GET /v1/tasks/{id}  (also written as verdict.md)
sde report <batch>                GET /v1/batches/{id}/report → output/reports/<batch>/
sde serve                         start the API service (the diagram's API Gateway)
sde generate "<topic>"            one draft + gates, no evaluation (scripts/generate.sh)
```

Exit codes: `0` success, `2` budget cap reached, `3` nothing claimable but unfinished work is
leased by another live process.

`summary.json` (all counts over **initial candidates**, not versions; `null` = not measured):

```json
{
  "batch_id": "b-20261117-01", "candidates": 10,
  "structural_pass": 10, "functional_pass": 9,
  "accepted_v1": 3, "accepted_total": 6,
  "rejected": {"exhausted": 2, "invalid": 1, "infra_unresolved": 1},
  "pass_count_histogram": {"0": 4, "1": 3, "2": 4, "3": 2, "4": 3, "5": 1},
  "valid_rollouts": 104, "invalid_rollouts": 7,
  "cost_usd": {"authoring": 11.2, "evaluation": 1.9, "total": 13.1, "per_accepted": 2.18},
  "wall_clock_s": 16420, "config_hash": "…"
}
```

(The numbers above are illustrative only.)

---

## 5. High-level design

A **distributed, asynchronous job-processing system** built from **workers on queues**. The API
service is the entry point; after that, every component is a worker that pulls jobs from its own
queue, does one stage, records the result, and **enqueues the next stage's job in the same
transaction**. Nothing orchestrates the stages from the middle: each stage hands off directly to
the next, and only the Analysis service makes the accept / revise / reject decision. FastAPI,
PostgreSQL, Redis sorted sets, independent worker fleets, containerized execution and S3. The
diagram is [`agent-evaluation-system_calibration_v3.excalidraw`](../agent-evaluation-system_calibration_v3.excalidraw)
(render: [`image.png`](../image.png), from before the 2026-11-17 relabel).

```
 Client ──► API service (FastAPI)   POST /v1/batches · GET status · GET results
              │  one txn: tasks (CREATED) + their v1 "generate" jobs
              ▼
        PostgreSQL (tasks · jobs; unpublished job rows = the outbox) ──► outbox publisher ──► Redis
                                                                                                │
   ┌──────────────────────────────┬──────────────────────────────┬──────────────────────────┤
   ▼ sde:generate                  ▼ sde:validate                  ▼ sde:rollout               ▼ sde:analyze
 ┌─────────────────────┐   ┌──────────────────────┐   ┌──────────────────────────┐   ┌───────────────────────────┐
 │ Generation workers  │   │ Build + Validate     │   │ Executor workers         │   │ Analysis service          │
 │ GLM-5.1: draft,     │   │ workers              │   │ 1 Harbor run per job in  │   │ waits for 5 valid rollouts│
 │ repair or revise    │   │ build · lint ·       │   │ an Execution Pod; upload │   │ of the version, then:     │
 │ → bundle to S3      │   │ oracle×2 · nop       │   │ the whole trace to S3    │   │ trace parser + GLM-5.1    │
 └─────────┬───────────┘   └─────┬──────────┬─────┘   └──────┬──────────┬────────┘   │ review → decide           │
           │ enqueue             │ pass:    │ fail:          │ always:  │ INVALID:   └──┬──────────┬──────────┬──┘
           │ validate            │ enqueue  │ enqueue repair │ enqueue  │ enqueue      │ accept   │ revise / │ reject
           ▼                     │ 5        │ generate (≤2)  │ analyze  │ retry        ▼ publish  │ repair   ▼
       sde:validate               ▼ rollouts ▼ or REJECT      ▼ (ping)   ▼ (same slot) output/   ▼          REJECTED
                             sde:rollout   sde:generate     sde:analyze  sde:rollout     accepted  sde:generate

 S3: bundles · LLM transcripts · raw traces · logs (write-once)
 Reconciler: reclaims expired leases, dead-letters, re-publishes, rebuilds Redis from PostgreSQL
 Analytics: read-only pass rates, cost, failure patterns, served through the API
 Inference egress gateway: FUTURE, not built; workers call the SDE inference API directly
```

### 5.1 Core architecture

| Component | Queue in → what it does → queue out | Module |
|---|---|---|
| **API service (FastAPI)** | Entry point, not a worker. `POST /v1/batches` writes the batch's tasks and their first `generate` jobs in one transaction and returns `202`. Read endpoints serve status, verdicts and reports (§4). It is also the diagram's "API Gateway": no separate gateway product sits in front of it. | `api/`; `sde` CLI calls the same functions |
| **Generation workers** | `sde:generate` → GLM-5.1 drafts a new task, repairs one that failed its gates, or revises one that was out of band (§6.1) → bundle to S3 → enqueue `validate`. | `generator/` |
| **Build + Validate workers** | `sde:validate` → build, structural validator, `harbor lint`, oracle × 2, nop (§6.3) → **pass:** enqueue 5 `rollout` jobs; **fail:** enqueue a repair `generate` (≤ 2 per version), else reject the task (`invalid`). | `validator/` |
| **Executor workers** | `sde:rollout` → one Harbor run in an Execution Pod, timeouts and resource limits enforced → upload the whole trace (trajectory, terminal output, verifier logs, result) to S3 → record outcome, reward, cost → **always** enqueue an `analyze` job (the "ping"); an `INVALID` run also enqueues a retry of the same slot. | `evaluation/executor.py` |
| **Execution Pods** | Run each agent attempt in an isolated container (Harbor task container, 1 CPU / 2 GB). | Harbor + Docker |
| **Analysis service** | `sde:analyze` → if the version doesn't have 5 valid rollouts yet, finish (nothing to do). Otherwise read the 5 traces from S3, run the trace parser and one GLM-5.1 review (§6.5), write `trace_metrics` and `failure` onto the rollout rows, and **decide** (§6.6): accept and publish, enqueue a revision or repair `generate`, or reject. The only component that judges difficulty. | `evaluation/analyzer.py`, `evaluation/trace_parser.py` |
| **PostgreSQL** | Source of truth: the 2 tables in §3. Every hand-off is a row inserted in the same transaction that records the previous stage's result. | `Store` |
| **Redis sorted sets** | One queue per worker type: `sde:generate`, `sde:validate`, `sde:rollout`, `sde:analyze`. Members are job ids; payload and state stay in PostgreSQL. | `Queue` |
| **Outbox publisher** | Adds committed job rows with `published_at IS NULL` to Redis. Duplicate publishes are harmless (stable ids). | `shared/jobs/jobqueue.py` |
| **S3** | Immutable task bundles, LLM transcripts, raw execution traces and logs. | `ArtifactStore` |
| **Reconciler** | Reclaims expired leases, dead-letters jobs that keep killing their worker, re-publishes unpublished jobs, rebuilds Redis from PostgreSQL. | `shared/jobs/reconciler.py` |
| **Analytics** | Read-only: pass rates, duration, cost and failure patterns by taxonomy dimension, pitfall, invalid reason and version. Serves the batch report. | `api/` report endpoint |
| **Inference egress gateway** | **Future.** The first deployment calls the provided SDE inference API directly. | none |

**Who may change a task.** Every task change is a compare-and-set on `(current_version, stage)`,
and each stage has exactly one owner, so two writers never race on the same step:

| Stage transition | Owner |
|---|---|
| create task (`CREATED`, v1 `QUEUED`) | API service |
| `QUEUED → GENERATING → BUILDING` (bundle written) | Generation worker |
| `BUILDING → VALIDATING → EVALUATING`, or repair back to `GENERATING`, or `INVALID → REJECTED (invalid)` | Build + Validate worker |
| `EVALUATING → BAND_OK → ACCEPTED`, `OUT_OF_BAND → v(n+1) QUEUED`, `REJECTED (exhausted / infra_unresolved)` | Analysis service |

### 5.2 End-to-end request flow

Client → API service → PostgreSQL + Redis → Generation workers → Build + Validate workers →
Executor workers → Execution Pods → S3 + PostgreSQL → Analysis service → Accept, or back to
Generation workers with a revision

1. **Request ingestion.** The client submits a batch. The API service writes the tasks and their
   `generate` jobs in **one PostgreSQL transaction**; the unpublished job rows are the outbox,
   and the outbox publisher adds them to Redis. The API returns `202` immediately.
2. **Task generation.** A generation worker claims the job, GLM-5.1 writes the task, the bundle
   is stored in S3 as an immutable, content-addressed version, and the worker enqueues `validate`.
3. **Build + validate.** A validate worker builds the environment and runs the gates. Pass → it
   enqueues **five** `rollout` jobs. Fail → it enqueues a repair `generate` (≤ 2 per version), or
   rejects the task once repairs are spent.
4. **Execution.** Executor workers run the attempts concurrently in isolated pods. Each one
   uploads its whole trace to S3 as soon as Harbor finishes, records the result, and enqueues an
   `analyze` job. Invalid attempts (timeouts, infra errors) are retried in the same slot, never
   scored.
5. **Analysis and calibration.** The `analyze` job that finds all five valid rollouts of the
   version does the work: trace parser, one GLM-5.1 review over all five, then the decision.
   1–3 passes and every failure the model's fault → `ACCEPTED`, bundle published. Too easy or
   too hard → a revision `generate` job for v(n+1) goes back on `sde:generate`, and the loop runs
   again from step 2. A failure blamed on the task → a repair revision. Still out of band at v3
   → `REJECTED (exhausted)`.

### 5.3 Reliability and scalability

- **Workers on queues:** every stage is a worker fleet on its own queue, so the API never waits
  on generation, validation, execution or analysis.
- **Independent scaling:** generation (model-quota bound), validation (CPU/Docker-build bound),
  execution (sandbox bound) and analysis (model-quota bound, one call per version) each scale on
  their own backlog.
- **Hand-offs can't be lost:** a worker records its result and enqueues the next job in **one
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
- **Backpressure:** claims are capped by Docker slots (validate + rollout) and the budget;
  queue priority is `analyze` > `rollout` > `validate` > `generate`, so started tasks finish before
  new ones are drafted.
- **Observability:** structured logs, job metrics, traces and the never-overwritten `jobs` history
  make every verdict auditable (§6.8).

### 5.4 Infrastructure and deployment

**Target (production).** Each component is an independently deployable workload on Kubernetes:
the API service, and one deployment per worker fleet (generation, validation, execution,
analysis) plus the reconciler. PostgreSQL and Redis provide state and scheduling; S3 holds large
artifacts. Execution runs on dedicated sandbox nodes, isolated from everything else.

**Default local setup.** Same components, same code paths, but the three stores are
backed locally and all roles run in one `sde` process (threads), so nothing beyond Docker is
needed:

| Interface | project (default) | Production |
|---|---|---|
| `Store` | SQLite file in WAL mode, `BEGIN IMMEDIATE` compare-and-set | PostgreSQL, `SELECT … FOR UPDATE` |
| `Queue` | Claimed straight from the `jobs` table with one `UPDATE … RETURNING` (§6.7); no outbox hop | Redis sorted sets fed by the outbox publisher; Lua claim + DB lease check |
| `ArtifactStore` | `./work/` content-addressed directories (write to temp, rename) | S3 |

`sde work --roles …` runs any subset of roles, so the same binary can be split into separate
fleets. An optional `deploy/compose.dev.yaml` runs the production backends locally; K8s
manifests are out of scope for the project (§1).

### 5.5 How each functional requirement is met

| FR | Path through the system |
|---|---|
| 1. Start batch | API service: tasks and their `generate` jobs in one transaction. |
| 2. Status | API service → PostgreSQL (`GET /v1/batches/{id}`, `sde status`). |
| 3. Kill/restart | All work is leased `jobs` rows; the reconciler reclaims expired leases; finished LLM calls and traces are found by key in S3 and reused (§6.7). |
| 4. Two copies | Every claim and commit is a compare-and-set on `lease_token`; deterministic job ids make each hand-off a no-op the second time; each task stage has one owner. |
| 5. Report | Analytics → `summary.json` + `report.md`. |
| 6. Spec + author | API service samples the slot → `generate` job → Generation worker → GLM-5.1 `TaskDraft` (§6.1–6.2). |
| 7. Gates | Generation worker enqueues `validate` → Build + Validate; failures become repair `generate` jobs (§6.3). |
| 8. Evaluate | Validate worker enqueues 5 `rollout` jobs → Executor → Execution Pods; classifier (§6.4). |
| 9. Explain failures | Executor pings `analyze` → Analysis service: trace parser + GLM-5.1 review → `trace_metrics` and `failure` on each rollout (§6.5). |
| 10. Accept / revise | Analysis service → `ACCEPTED`, or a revision `generate` job back on `sde:generate` (§6.6). |
| 11. Publish | Analysis service copies the bundle + `verdict.md` to `output/accepted/<name>/` inside the ACCEPTED transition. |
| 12. Inspect | API service → `GET /v1/tasks/{id}` / `sde inspect` / `verdict.md` (§6.8). |

---

## 6. Deep dives and main code paths

### 6.1 Authoring a task (GLM-5.1)

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

### 6.2 Difficulty profile (from `TASK_GENERATION_RULES.md`)

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

The v1 profile starts from a single default ("medium-hard": 2 interacting components, 3
constraints with at least one interaction, root cause not stated, 2 dependent subgoals), tuned
after the M2 pilot shows where `glm-5.3-flash` actually lands.

### 6.3 Validation gates (in order, cheapest first)

| # | Gate | Pass condition | On failure |
|---|---|---|---|
| 1 | `TaskDraft` validators | no errors | repair |
| 2 | `validator/validate.py` | exit 0 | repair |
| 3 | `harbor lint --json` | no `agent-image-test-leak`, `unpinned-image`, `unpinned-python-dependency`, `root-agent` | repair; other findings are warnings |
| 4 | oracle trial ×2 | reward 1 both times | repair (flaky = fail) |
| 5 | nop trial | reward exactly 0, no verifier exception | repair |

A verifier crash or missing reward is **not** nop = 0. Docker build failures show up in gates 4–5
as Harbor environment exceptions and are repaired with the build log. Gates 2–5 run inside one
`validate` job, which holds one Docker slot; its Harbor dirs go under `traces/<job_id>/oracle1`,
`…/oracle2` and `…/nop`, so a reclaimed validate job adopts finished steps instead of rerunning
them. Each step's result is recorded in the validate job's `result.gates`.

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
slot has used its retries without a valid result, the Analysis service sets the version
`UNRESOLVED` and the task `REJECTED (infra_unresolved)`, never scored as hard. If invalid rollouts pile up
across tasks (e.g. gateway outage: more than 5 in 10 minutes), the Executor pauses.

### 6.5 Why each failure is the model's fault (Analysis service)

The Analysis service runs this once per version, when its five valid rollouts are in, reading
the traces the executors uploaded to S3. For every rollout it extracts `trace_metrics` with code
(LLM calls, tokens, last tool calls, files the agent wrote, duration). For every `VALID_FAIL` it
builds an **evidence record** with code first:

- failing test IDs and assertion messages from the verifier output;
- the agent's last N commands and its final message from the trajectory;
- which files the agent changed, compared with the files `solve.sh` changes.

Then one GLM-5.1 call (authoring model, allowed for diagnosis; no new model) reviews all five
rollouts together, failures next to passes, with the task instruction and the hidden `TaskSpec`.
Per failure it returns `{explanation, attribution: model|task, pitfall_matched, cited_evidence}`.
It must cite evidence ids; an explanation that cites nothing real, or a review that can't run
(GLM-5.1 aborts strict-JSON output fairly often, so it is retried, then falls back to plain JSON
mode), is recorded as `unexplained`: kept, never trusted. The call is cached per version, so a
reclaimed analyze job never pays twice.

- `attribution = task` (ambiguous instruction, environment bug, test checking something not
  asked for) → the version is `INVALID` and goes to a `repair_ambiguity` revision. It doesn't
  count as a valid difficulty measurement.
- Accepted tasks must have **every** failure attributed to the model, with evidence. That is the
  "know why it fails" requirement.
- `pitfall_matched` checks whether failures hit the intended decision points (§6.2) or something
  incidental.

### 6.6 Aggregation and revision (Analysis service)

When a version has 5 valid rollout jobs under one bundle sha and one config hash, the Analysis
service decides (an `analyze` job that finds fewer finishes with `action: wait`):

```
pass_count in 1..3, all failures model → BAND_OK, task ACCEPTED, publish
any failure attribution task      → repair revision (repair_ambiguity), not a difficulty measurement
pass_count 0 or 4..5, n < 3       → OUT_OF_BAND, advance to v(n+1) and enqueue its revision job
pass_count 0 or 4..5, n = 3       → OUT_OF_BAND, task REJECTED (exhausted)
```

Moving to v(n+1) is one compare-and-set, `UPDATE tasks SET current_version=n+1, stage='QUEUED'
WHERE id=? AND current_version=n AND stage='EVALUATING'`, in the same transaction as inserting the
`generate` job with its deterministic id, so two analyze jobs that both see five valid rollouts
can't both create it: one wins, the other finds the stage already moved and finishes.

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
- **Publishing** to `output/accepted/` happens inside the Analysis service's `ACCEPTED`
  transition: copy to temp, rename. A second analyze job sees `ACCEPTED` and skips.

Tests (§8, M3): kill `-9` at random points over 20 runs, check no rollout ran twice (Harbor
dirs = rollout job rows) and the final counts are unchanged; run two `sde work` processes on one batch
and check the uniqueness invariants.

### 6.8 Audit records

Per task, `sde inspect <task>` and `verdict.md` (also written into the accepted bundle) show:

1. **Decision** with its rule: "ACCEPTED: v2 passed 2/5 valid rollouts (band 1–3)".
2. **Spec**: taxonomy slot, difficulty profile, hidden `TaskSpec` (root cause, constraints,
   intended pitfalls).
3. **Versions**: per version, origin/operator, bundle sha, gate table with log paths, pass
   pattern.
4. **Rollouts**: table of slot, retry, outcome, reward, duration, cost, trace; invalid retries
   shown with their exception type.
5. **Failures**: per failed rollout, the evidence excerpts, explanation and attribution, as
   written by the Analysis service; plus the analyze job's summary of why the version was
   accepted, revised or rejected.
6. **Cost and time**: LLM vs. eval, wall time per stage.

Everything links to raw files (`work/llm/…`, `work/traces/…`), so a reviewer can check any claim
against the source. The task's job rows, ordered by time, are the full timeline
(`sde inspect --timeline`).

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
- **Hard rule:** no two tasks in a batch share (domain, task type, environment).
- **Lexical novelty:** word 5-gram MinHash Jaccard of instruction + `TaskSpec.root_cause` against
  every earlier bundle and the six `examples/`. Above 0.3 → new pitch. This is a heuristic, not
  semantic novelty (that's the deferred embedding feature).
- **Report:** distribution of every dimension across the batch.

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
  in the generate job's `result.llm_calls`). Missing values are recorded as
  null, not estimated.
- A budget check runs before every paid action: stop claiming new work at the batch cap and exit 2.
- Scheduling favors finishing what's started: analyze before rollouts, rollouts before new
  validations, and validations before new drafts (`priority`). In the one `sde:generate` queue,
  repairs and revisions go ahead of new drafts. A new candidate is only drafted when fewer than 2 versions are
  waiting for Docker slots.

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
| D1 | Simplify the spec's data model (10 tables, Redis outbox) | **Adopt this PRD's 2 tables** (`tasks`, `jobs`): the job row is the queue entry, the rollout record, the audit history and the outbox; versions are `(task_id, version)` on jobs; a batch is a label. **Decided 2026-11-17:** workers on queues hand off directly (§5); no central orchestrator; the Analysis service owns accept / revise / reject. Same guarantees (leases, fencing, never-overwritten history, idempotent ids). Update the spec's §4–5 to point here. |
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
4. **M3 robustness:** SQLite store (2 tables), job leases, adoption, `sde run/work/status/inspect`, kill and
   two-copy tests. (Built before the real batch so that batch runs on it.)
5. **M3b workers on queues + Analysis service:** each worker hands off its next stage in its
   settle transaction; executors ping `sde:analyze`; the Analysis service runs the trace parser
   and one GLM-5.1 review per version, writes `failure` / `trace_metrics`, and decides accept /
   revision / repair / reject. Owner per stage transition as in §5.1.
6. **M4 revision loop:** operators, v2/v3 through the generation worker's revision path, before/after in the report.
7. **M5 batch:** 10+ candidates, `sde report`, update `CHANGELOG.md` with measured results.
8. **M6 human-likeness:** blind rubric comparison with `examples/`; optional compose backend.
