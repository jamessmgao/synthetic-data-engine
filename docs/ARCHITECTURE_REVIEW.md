# Architecture review: the agentic authoring migration

A manager's review of the migration, started on `main` at 135b82b (INF1, EX1, TB1, API1
merged) and kept up to date through the end of 2026-11-17. Each finding has an owner, the
fix, and its status. Fixes were assigned to the session that owns the file
(docs/AGENTIC_AUTHORING_PLAN.md §4). The sessions: -18 infrastructure (also took TB1-TB2),
-75 api-service (also took TB3 and the tuning), -c0 executor-worker (the integrator).

Severity: **bug** = wrong behavior in a reachable path; **cost** = wastes paid model time or
Docker slots; **eff** = wasted CPU, DB or I/O; **design** = a structural improvement;
**quality** = weaker tasks.

Status: **done** = merged to `main`; **open** = not fixed (see docs/TODO.md).

## R: found while reviewing the first merges

| # | Sev | Where | Problem | Fix | Owner | Status |
|---|---|---|---|---|---|---|
| R1 | bug, cost | shared/jobs/worker.py `run_one.beat`; evaluation/executor.py `_heartbeat` | A heartbeat that raised (a DB error) killed the heartbeat thread; the lease lapsed and a 60-minute authoring job ran twice | retry on exception, lost only on a refused heartbeat; agent lease `SDE_AGENT_LEASE_TTL_S=1800`; the same fix in the Executor's own heartbeat | -c0 | **done** 135b82b, 60e561b |
| R2 | bug | shared/jobs/worker.py `draft_fn` | in agent mode every create/revise went to `run_create_job`, including world-less ones (raw prompt, v1 spec, in-flight one-shot tasks) | dispatch per job on `input.world_sha256` | -c0 | **done** 135b82b |
| R3 | bug | shared/jobs/handoffs.py | gate repairs of a world-built task lost the world (→ one-shot) | carry world_sha256 through validate → repair | -c0 | **done** 135b82b |
| R4 | design | evaluation/calibration.py `choose_mutations` | pass-review cues were answered with a distractor (the task still leaks); the size-adding fallback; needle false positives on common words | `remove_cues` first when cues are found; fallbacks `introduce_global_invariant` → `generate_counterexample`; phrase rules on the analysis only, code patterns on commands; every choice records {operator, rule, evidence} | -c0 | **done** 3a8d040 |
| R5 | bug | handoffs.after_world | an OK outcome without a world sha left the task in WORLD_BUILDING forever | reject INVALID | -18 | **done** 5525698 |
| R6 | cost | handoffs.after_world | the first authoring failure (timeout, gateway error) rejected the task | `world-retry<k>` (same input) up to MAX_REPAIRS_PER_VERSION, with failure_kind infra/author | -18 | **done** 5525698 |
| R7 | cost | models.DOCKER_JOB_TYPES | hour-long authoring jobs could hold every Docker slot; one-shot drafts in agent mode took a slot | `jobs.needs_docker` set per job at enqueue; `SDE_AGENT_SLOTS` (default DOCKER_SLOTS-1) | -18 | **done** f8f6e90 |
| R8 | cost | worker.py world_fn / draft_fn | the authoring trace was saved only on success, so a retry re-paid | `save_trace` in finally; the harness adopts finished trials | -18 | **done** df3929e, 272af87 |
| R9 | bug, cost | authoring cost | the gateway's glm-5.1 `usage.cost` is a flat 5e-05 placeholder | cost from tokens × price; the gateway figure kept as `gateway_cost_usd` | -18 | **done** 272af87 |
| R10 | bug | shared/taskformat/world_check.py | `-v` bind mounts break through the Docker socket; image tags collided across concurrent checks | a derived check image (COPY, no mounts); unique tags | -18 | **done** 3601f5c, 272af87 |
| R11 | eff, cost | authoring containers | containers and images the agent starts through the socket outlived the trial, uncapped | `sde.job=<id>` labels on every build and run, cleanup in finally, world containers capped at 1 CPU / 2 GB | -18 | **done** 977d7b1 |

## N: found in the review of main 135b82b

| # | Sev | Where | Problem | Fix | Owner | Status |
|---|---|---|---|---|---|---|
| N1 | bug | shared/jobs/reconciler.py `reconcile` | a dead-lettered `world` or `world_check` job didn't reject its task (the reject branch checked `(*GENERATION_TYPES, VALIDATE)` only), so the task stayed in WORLD_BUILDING / WORLD_CHECKING and its batch never finished | reject infra_unresolved for every agent generation and gate type; unit test | -c0 | **done** 02add1e |
| N2 | bug (report) | evaluation/verdict.py, api/service.py version view | a version's origin and operators were read from its first create/revise job, so every mutated agent-mode version rendered as "initial" | `verdict.version_input()`: the version's first non-repair world/create/revise; the API view also shows mutation_operators | -c0 | **done** 02add1e |
| N3 | eff | shared/jobs/jobqueue.py `claim` | with the machine's Docker slots full, every poll still ran a locked claim transaction per Redis candidate | count the host's Docker / agent leases once per poll and skip full types | -18 | **done** f8f6e90 |
| N4 | design | shared/jobs/jobqueue.py `_insert_followup` | follow-ups inherited the parent's bundle/config pins (a too-easy `world` job for v n+1 pinned to v n's bundle) and an empty `input` became the parent's | inherit pins only within the same version; a stated input (even `{}`) is used as given | -18 | **done** d436107 |
| N5 | design | shared/storage/sql.py Postgres `tx` | every transaction takes one global `pg_advisory_xact_lock` | kept: simple and correct at this size; the load test shows no DB waits at 8 runners. Revisit with `FOR UPDATE SKIP LOCKED` for a larger fleet | none | noted |
| N6 | cost | validator/gates.py leak gate in `validate` | the leak review, a multi-minute glm-5.1 call, held a Docker slot while waiting | run it at the end of the create job; validate adopts the recorded review by bundle sha; a leak → repair (see L2) | -75 | **done** d6ab4b3, 0376e02 |
| N7 | eff | shared/storage/artifacts.py S3 `_upload_dir` | serial file-by-file uploads; authoring artifacts unbounded | 8 upload threads; authoring artifacts capped at 5k files / 50 MB with author feedback; the leak review registered with S3 | -18 | **done** a796fbd |

## L: found later (P1, the AWS diagnosis, the load test)

| # | Sev | Where | Problem | Fix | Owner | Status |
|---|---|---|---|---|---|---|
| L1 | bug | generator/authoring_harness.py | the harness looked for artifacts in `<trial>/artifacts`; Harbor 0.26 writes `<trial>/artifacts/logs/artifacts`, so every agent job reported "no world"; the fake Harbor in the tests used the wrong layout | read Harbor's `artifacts/manifest.json`, fall back to the known layouts; the fake Harbor writes the real layout | -18 | **done** 6d231bb |
| L2 | bug | shared/jobs/handoffs.py `after_generation` | any FAILED create rejected the task, so a leak found at create (N6) killed it | an author failure with a bundle → `repair<k+1>` (same counter as validate's repairs); infra failures as before | -75 | **done** 0376e02 |
| L3 | bug | RDS on AWS (db.t4g.micro) | out of connections ("reserved for rds_reserved") with 4 runners; the resize was refused (free tier) | a bounded psycopg pool per process (`SDE_DB_POOL_MAX`, 2 on AWS), heartbeats included | -18 | **done** 1d5d869 |
| L4 | bug | shared/storage/sql.py `claim` crash isolation | an expired job ("suspect") could only be claimed by a process with no live lease, so on a busy machine it waited forever (aws-prompt-1's rollout waited hours) | after `SDE_SUSPECT_MAX_WAIT_S` (600 s) any process may claim it, still isolated | -18 | **done** f743fd1 |
| L5 | bug | evaluation/harbor.py orphan check | shelled out to `ps`, missing in the runner image, so a reclaimed rollout settled INVALID | read `/proc/<pid>/cmdline` | -c0 | **done** (merged with 267390f) |
| L6 | bug | deploy/compose.dev.yaml | passed only 3 SDE_* settings, so a local stack ran with no budget cap and default concurrency; `environment:` defaults then shadowed env_file | every app service reads deploy/make.env + deploy/local.env; nothing set in `environment:`; a test fails on a missing or shadowed setting | -18 | **done** b31ff23, 12d890d |
| L7 | gap | shared/jobs/tasks.py, api | no way to stop a batch: stopping aws-b1 needed manual rejects labeled infra_unresolved | CANCELLED status, `POST /v1/tasks/{id}/cancel` and `/v1/batches/{id}/cancel`, scripts/relabel_cancelled.py | -75 | **done** d8ca8dc |
| L8 | quality | prompts/task_author.md, validator/gates.py leak review | P1's instruction described the counterexample (where the crash hook fires, the exact time step) and nearly named the fix; the leak review called it clean, because its definition allowed documented hooks and "required behavior" | prompts forbid schedules, hook positions and fix shapes (P1 as the BAD example); the review judges paraphrased giveaways with the world's real fix as input, plus a blind fix prediction | -75 | **done** 4baa6ac, 152b0b6 |
| L9 | quality | prompts/world.md, task_author.md | P1's fix was one file and one index, with a documented crash hook and one replayed test | worlds: a local patch isn't enough, no documented hooks, a tempting wrong fix; tests: one per invariant over ≥20 seeds, a flake check, `# invariant: N` enforced | -75 | **done** f8c9081, a124f4a |
| L10 | infra | jobqueue claim, AWS runners | CPU saturates before memory (load 6-8 on 2 vCPU, memory 75%+ free); tmux send-keys flakes under CPU load; bursts landed on 6 of 8 runners | CPU guard (90% of the 1-min load average), claim pacing (`SDE_DOCKER_CLAIM_SPACING_S`, 10 s on AWS) | -18 | **done** b31ff23, 41b25be |
| L11 | eff | shared/taskformat/materialize.py `TEST_SH` | every verifier run downloads uv + Python + pytest (~15 s CPU, and a network dependency at verify time) | proposed: a root-only verifier venv baked at image build | -18 | open (load test) |
| L12 | infra | evaluation/harbor.py | a solver's model call can hang until the 60-minute agent timeout (P1 rollout 2) | a per-request timeout and retry for the solver's model calls | -c0 | open (TODO) |
| L13 | design | evaluation/calibration.py | world_check hardness results don't reach the mutation round | tiebreak-only rule (the user's choice) | -c0 | open: parked (TODO) |

## Architecture notes

- **One job per authoring step is right.** world, world_check, create and validate are separate
  rows, so a failed create retries without rebuilding the world, a world can be reused by
  several tasks, and each step's cost and time are visible.
- **Fencing is consistent.** heartbeat / settle / release are compare-and-set on the lease
  token, and every handoff is a CAS on (version, stage), so duplicate runs waste money but
  never corrupt state. The duplicate-run risks (R1, R8, L4) are fixed.
- **Mode is per job, not per process** (R2), which makes switching SDE_AUTHOR_MODE safe for
  in-flight tasks. Keep this rule for any new job type.
- **Failures loop back, with limits.** Every author-side failure (a leaking draft, a failed
  gate, a world that doesn't check out) becomes a repair job with feedback, capped by
  MAX_REPAIRS_PER_VERSION; infra failures retry; reject is the last resort.
- **The cost split, measured** (P1): a world is about $0.51 and 5 minutes, a task about $2.39 and
  22 minutes, 5 solver runs about $0.35. Authoring dominates spend; the budget estimates in
  shared/storage/budget.py (world $1, create $4) are about 2× the measured figures.
- **What limits throughput** (the load test): CPU from container and verifier work, not the
  database, Redis or S3. The model's wait dominates a real job's wall time.
