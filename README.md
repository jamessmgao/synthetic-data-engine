# synthetic-data-engine

A closed-loop synthetic data engine: agents build executable bug-worlds, author tasks from
them, a pinned solver calibrates their difficulty, and a durable job scheduler runs the whole
loop at scale.

The output is RL training environments in the Harbor / Terminal-Bench 2 task format. A task is
kept only when it's **learnable**: 1–3 of 5 agent runs pass with `terminus-2` +
`glm-5.3-flash` at `reasoning_effort=high`.

## What it does

### 1. Builds worlds

A terminus-2 + glm-5.1 agent session (in the `sde-authoring` image) builds an executable
**world**: a small buggy system with invariants, a reproducible counterexample, a real fix and
a plausible wrong fix. A deterministic **world_check** proves it: the world builds, the repro
fails, the fix passes, the symptom is visible, the fix isn't local, and the wrong fix fools the
symptom (hardness checks set by `SDE_WORLD_HARDNESS`). A failing world goes to world-repair
(≤2). When a task is too easy, the **world** is mutated, not the wording. See
[docs/WORLD_CONSTRUCTION.md](docs/WORLD_CONSTRUCTION.md) and
[docs/AGENTIC_AUTHORING_PLAN.md](docs/AGENTIC_AUTHORING_PLAN.md).

### 2. Creates tasks

A second agent session writes a **task** from the world: the instruction, Dockerfile, tests
and reference solution. The validate gates check it: structural, lint, oracle ×2, nop,
wrong_fix, and a blind leak review. A failing task goes to repair (≤2). See
[docs/VALIDATION_GATES.md](docs/VALIDATION_GATES.md).

A one-shot mode (`SDE_AUTHOR_MODE=oneshot`, the default) drafts the whole bundle in one
strict-JSON glm-5.1 call instead. It's the baseline, and the fallback for raw prompts and v1
specs. The mode is chosen per job: a create or revise job with a `world_sha256` runs the agent
author (`shared/jobs/worker.py:draft_fn`).

### 3. Calibrates difficulty

The **Executor** runs 5 agent runs on the pinned solver (terminus-2 + glm-5.3-flash,
reasoning high). The **Trace Parser** decides whether each failure is the model's fault or the
task's; timeouts and infrastructure errors never count as model failures. Calibration then
routes the task:

- 1–3 of 5 valid passes, failures blamed on the model → **accepted** (published);
- too easy (4–5/5) → **world mutation** (v+1): 1–2 operators aimed at what passed;
- too hard (0/5) → **revise easier** on the same world;
- the task at fault → **repair**;
- at most 8 versions (`SDE_MAX_VERSIONS`), then rejected.

See [docs/EVALUATION.md](docs/EVALUATION.md).

### 4. Schedules jobs

Every arrow in the loop is a job on a durable, Postgres-backed scheduler:

- the `jobs` table is the queue, the outbox and the audit log; Redis sorted sets (one per job
  type) are only a dispatch index;
- workers are stateless; claims are leased and fenced by a token, with heartbeats that survive
  store errors (`shared/jobs/worker.py:run_one`, `evaluation/executor.py:Executor._heartbeat`);
- hand-offs commit the next job in the same transaction as the settle
  (`shared/jobs/handoffs.py:after_world`, `after_world_check`, `after_generation`, `after_validate`);
- a reconciler re-publishes lost queue entries and dead-letters jobs that keep failing (and
  rejects their task);
- Docker slots, agent slots, a memory guard and a CPU guard gate each claim
  (`shared/jobs/jobqueue.py:JobQueue.claim`).

Any service can be killed, restarted or run as several copies without losing or re-paying
work. The same code runs locally on Docker Compose (or a local k3d cluster) and in production
on AWS: K3s on EC2 instances with KEDA autoscaling the Executor, RDS Postgres, ElastiCache
Redis and S3. The backends come from the environment: SQLite or Postgres, the jobs table or
Redis, files or S3.

## The flow

```
POST /v1/tasks | /v1/batches
        │
        ▼  agent mode (spec v2: family, mechanism, shape, dial)
  world ──► world_check ──fail──► world-repair (≤2) ──► … ──► REJECTED invalid
  (agent)   (gate: build, repro fails, fix passes, symptom visible,
             fix not local, wrong fix fools the symptom; SDE_WORLD_HARDNESS)
        │ ok
        ▼
  create ──► validate ──fail──► repair (≤2, also when create's leak review fails)
  (agent from the world,  (structural · lint · oracle×2 · nop · wrong_fix · leak)
   or one-shot)
        │ ok
        ▼
  agent runs ×5 (terminus-2 + glm-5.3-flash, high) ──► analyze (Trace Parser)
        │
        ├─ 1–3 of 5 valid passes, failures blamed on the model ──► ACCEPTED (published)
        ├─ too easy (4–5/5) ──► world mutation (v+1): 1–2 operators aimed at what passed
        ├─ too hard (0/5)  ──► revise "easier" on the same world
        ├─ the task blamed ──► revise "repair_ambiguity"
        └─ out of band at MAX_VERSIONS (8) ──► REJECTED exhausted
```

The architecture diagram is [`synthetic-data-engine.excalidraw`](synthetic-data-engine.excalidraw);
the original design spec is [`synthetic_data_engine_spec.md`](synthetic_data_engine_spec.md).

## Services

| Service | Entry point | Consumes | Code |
|---|---|---|---|
| API | `python -m api` | HTTP | `api/app.py:create_app`, `api/submit.py:create_task` / `create_batch` |
| Generation Service | `python -m generator.service generation` | `world`, `create`, `revise` | `shared/jobs/worker.py:serve_generation`, `world_fn`, `draft_fn`; `generator/author.py:run_world_job`, `generator/author_task.py:run_create_job`, `generator/worker.py:run_generate_job` (one-shot) |
| Build + Validate | `python -m validator.service validate` | `validate`, `world_check` | `validator/gates.py:run_validate_job` (`gate_wrong_fix`, `gate_leak`), `shared/taskformat/world_check.py:run_world_check_job` |
| Executor | `python -m evaluation.executor` | `rollout` (agent runs) | `evaluation/executor.py:Executor`, `evaluation/harbor.py:run_trial`, `evaluation/classify.py` |
| Trace Parser | `python -m evaluation.trace_parser_service` | `analyze` | `evaluation/trace_parser_service.py:TraceParserService.analyze` / `next_job`, `evaluation/trace_parser.py:parse` / `review_passes`, `evaluation/calibration.py:choose_mutations` |
| Reconciler | `python -m shared.jobs.reconciler` | (timer) | `shared/jobs/reconciler.py:reconcile`: re-publishes lost queue entries, dead-letters poison jobs (and rejects their task), sweeps staging |

State: Postgres (`tasks` + `jobs`), Redis (one sorted set per job type), S3 (bundles, worlds,
drafts, traces). A spend cap can stop new paid claims (`shared/storage/budget.py:claim_filter`).

## Quick start

```bash
make deps-up              # Postgres + Redis + S3 only (for the tests)
make test-all             # every suite, incl. e2e on SQLite and the compose cluster (no model calls)
make authoring-image      # build sde-authoring:dev (agent mode)
cp deploy/local.env.example deploy/local.env   # then set what you need (below)
make up-agent             # the whole stack in agent mode (or `make up` for one-shot)
curl -s localhost:8000/healthz                 # status, DB, budget cap and spend
```

The stack reads the gateway key from the repo's `.env` (`OPENROUTER_API_KEY`,
`OPENROUTER_API_BASE`; never committed). Every app container reads `deploy/local.env`.
Unset means the code's default:

| Setting | Default | Meaning |
|---|---|---|
| `SDE_AUTHOR_MODE` | `oneshot` | `agent` = world-first agent authoring; `oneshot` = one glm-5.1 call per draft |
| `SDE_AUTHORING_IMAGE` | `sde-authoring:dev` | the image agent sessions run in (`make authoring-image`) |
| `SDE_GENERATION_CONCURRENCY` | 8 | Generation Service claim threads per process |
| `SDE_DOCKER_SLOTS` | 4 | Docker jobs per machine (validate, world_check, agent runs, agent sessions) |
| `SDE_AGENT_SLOTS` | `DOCKER_SLOTS − 1` | of those, hour-long agent sessions (world, create/revise from a world) |
| `SDE_MIN_FREE_MEMORY_PCT` | 20 | below this free memory no Docker work is claimed (the real limit) |
| `SDE_MAX_CPU_LOAD_PCT` | 90 | above this CPU load no Docker work is claimed |
| `SDE_DOCKER_CLAIM_SPACING_S` | 0 | minimum seconds between Docker claims on one machine (claim pacing) |
| `SDE_AGENT_LEASE_TTL_S` | 1800 | lease length for agent jobs (other jobs: `SDE_LEASE_TTL_S`, 600) |
| `SDE_SUSPECT_MAX_WAIT_S` | 600 | how long a job whose worker died waits for crash isolation before anyone may take it |
| `SDE_BUDGET_USD` | none | global spend cap; batches also take `budget_usd` |
| `SDE_MAX_VERSIONS` | 8 | revisions per task before REJECTED exhausted |
| `SDE_WORLD_HARDNESS` | `warn` | world_check's hardness checks: `off` / `warn` (recorded, not failing) / `enforce` / `strict` |
| `SDE_LEAK_REVIEW` / `SDE_LEAK_BLIND` | 1 / 1 | the leak review in create and validate / its blind fix prediction |
| `SDE_CREATE_TIMEOUT_S` / `SDE_WORLD_TIMEOUT_S` | 1200 / 1800 | agent session time boxes |
| `SDE_DB_POOL_MAX` | 4 | Postgres connections per process (bounded pool) |
| `SDE_CANCEL_GRACE_SEC` | 30 | how long a cancelled running job may take to stop |
| `SDE_MOCK_LLM` | unset | load tests only: fake model calls. **Never set for a real batch.** |

`deploy/local.env.example` lists every setting the code reads; `tests/deploy/test_compose_env.py`
enforces that.

### Submit, watch, cancel

```bash
H='X-Api-Key: <SDE_API_KEY>'
curl -s localhost:8000/v1/families -H "$H"                          # areas, mechanisms, environments
curl -s -X POST localhost:8000/v1/batches -H "$H" -H 'Content-Type: application/json' \
  -d '{"batch_id": "b1", "count": 5, "area": ["database_consistency", "compilers_parsers"], "dial": 3, "budget_usd": 50}'
curl -s localhost:8000/v1/batches/b1 -H "$H"          # per-task status, version, pass pattern, cost
curl -s localhost:8000/v1/batches/b1/report -H "$H"   # summary.json + the yield block
curl -s -X POST localhost:8000/v1/batches/b1/cancel -H "$H"   # stop the batch: CANCELLED, spend kept
```

Single tasks: `POST /v1/tasks` takes an empty body (sampled spec), a `prompt`, a `spec` or a
`raw_prompt`. Then `GET /v1/tasks/{id}` (versions, worlds, gates, mutations) and
`POST /v1/tasks/{id}/cancel`. `scripts/run_tasks.py --batch b1 --count 10` runs a whole batch: it creates the batch, follows it and writes
`output/reports/<batch>/summary.json`. Accepted tasks are published to
`output/accepted/<task>/` with a `verdict.md` (`evaluation/verdict.py`).

### Deploy

- **Local:** `make test-infra` runs SQLite, Postgres, Postgres+Redis and S3. `make up` /
  `make up-agent` runs the whole stack in Docker Compose. `make k8s-test` runs the chaos test
  on a local k3d cluster.
- **AWS:** Terraform under `deploy/terraform` creates the EC2 instances, RDS Postgres,
  ElastiCache Redis, S3 and SSM-only access, operated with `deploy/aws_ops.py` / `make aws-*`.
  `make aws-k8s-up` installs K3s and KEDA across the EC2 instances; `make aws-k8s-deploy
  OVERLAY=aws-prod` applies the production overlay (`infra/k8s/overlays/aws-prod`). Agent
  mode: `make aws-author-mode MODE=agent`.

See [docs/DEPLOY.md](docs/DEPLOY.md) for deployment and every runtime setting,
[docs/AGENT_MODE_DEPLOY.md](docs/AGENT_MODE_DEPLOY.md) for agent mode,
[docs/CAPACITY.md](docs/CAPACITY.md) for the cluster's size, concurrency limits and cost, and
[docs/LOAD_TEST.md](docs/LOAD_TEST.md) for the load test with mocked models. CPU is the
bottleneck; there were 0 INVALID in 230 agent runs.

## Docs

| Doc | What it holds |
|---|---|
| [`docs/PRD.md`](docs/PRD.md) | requirements, data model, API, the services-on-queues design, deep dives |
| [`docs/AGENTIC_AUTHORING_PLAN.md`](docs/AGENTIC_AUTHORING_PLAN.md) | the world-first plan: job types, contracts (spec v2, worlds, gates, mutation inputs), status |
| [`docs/WORLD_CONSTRUCTION.md`](docs/WORLD_CONSTRUCTION.md) | the method: worlds, invariants, counterexamples, mutation operators |
| [`docs/HILLCLIMBING_ALGORITHM.md`](docs/HILLCLIMBING_ALGORITHM.md) | the sliding-window tuning loop on the production cluster |
| [`docs/ANALYSIS.md`](docs/ANALYSIS.md) | every pilot and live run: outcomes, cost, time, bugs found (§11: the deployed runs, §12: P1, the aws-b1 diagnosis, the nop flake) |
| [`docs/API.md`](docs/API.md) | every route with its request and response shapes |
| [`web/README.md`](web/README.md) | the dashboard: status and results of every batch, task and job (`make ui`, `make ui-aws`) |
| [`docs/VALIDATION_GATES.md`](docs/VALIDATION_GATES.md) | the validate gates (wrong_fix, leak) and world_check, incl. the hardness checks |
| [`docs/TASK_GENERATION_RULES.md`](docs/TASK_GENERATION_RULES.md) | what a good task is: the authoring rules the prompts enforce |
| [`docs/EVALUATION.md`](docs/EVALUATION.md) | the Executor, the Trace Parser, the mutation rules and their names, the verdict format |
| [`docs/ARCHITECTURE_REVIEW.md`](docs/ARCHITECTURE_REVIEW.md) | every R/N/L review finding, with its resolution and commit |
| [`docs/TESTING.md`](docs/TESTING.md) | test suites, the e2e harness, the agent-mode (INT) scenarios |
| [`docs/DEPLOY.md`](docs/DEPLOY.md) | deployment (Compose, k3d, K3s on AWS) and every runtime setting |
| [`docs/CAPACITY.md`](docs/CAPACITY.md) | concurrency limits, throughput, utilization and cost |
| [`docs/AGENT_MODE_DEPLOY.md`](docs/AGENT_MODE_DEPLOY.md) | running agent mode locally and on AWS |
| [`docs/LOAD_TEST.md`](docs/LOAD_TEST.md) | the AWS load test with mocked models |
| [`docs/K8S_PLAN.md`](docs/K8S_PLAN.md), [`docs/K3S_PLAN.md`](docs/K3S_PLAN.md) | the move from Compose to K3s / KEDA |
| [`docs/WORKER_INTERFACE.md`](docs/WORKER_INTERFACE.md) | the job-function contract between services and task-builder code |
| [`docs/TODO.md`](docs/TODO.md), [`docs/LINKS.md`](docs/LINKS.md) | open work; references |
| [`synthetic_data_engine_spec.md`](synthetic_data_engine_spec.md) | the original implementation spec |
| [`CHANGELOG.md`](CHANGELOG.md) | every change, with commits |
