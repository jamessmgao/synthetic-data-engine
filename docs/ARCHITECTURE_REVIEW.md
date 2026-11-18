# Architecture review: main 135b82b (agentic authoring migration)

A manager's review of `main` at 135b82b (INF1, EX1, TB1, API1 merged), done while
TB2, TB3 and INT were in progress. Each item has an owner and a status; the fixes
are assigned to the session that owns the file (docs/AGENTIC_AUTHORING_PLAN.md §5).

Severity: **bug** = wrong behavior in a reachable path; **cost** = wastes paid
model time or Docker slots; **eff** = wasted CPU, DB or I/O; **design** = a
structural improvement.

## Found and fixed or assigned in review (before this doc)

| # | Sev | Where | Problem | Fix | Owner | Status |
|---|---|---|---|---|---|---|
| R1 | bug, cost | shared/jobs/worker.py `run_one.beat` | A heartbeat that raised killed the heartbeat thread; the lease lapsed and a 60-minute authoring job ran twice | retry on exception, lost only on a refused heartbeat; agent lease 1800s | -c0 | **merged** 135b82b |
| R2 | bug | shared/jobs/worker.py `draft_fn` | in agent mode every create/revise went to `run_create_job`, including world-less ones (raw prompt, v1 spec, in-flight one-shot tasks) | dispatch per job on `input.world_sha256` | -c0 | **merged** |
| R3 | bug | shared/jobs/handoffs.py | gate repairs of a world-built task lost the world (→ one-shot) | carry world_sha256 through validate → repair | -c0 | **merged** |
| R4 | design | evaluation/calibration.py | pass-review cues answered with a distractor (the task still leaks) | `remove_cues` operator first; size-adding fallback dropped; needle false positives | -c0 | **merged** 3a8d040 |
| R5 | bug | handoffs.after_world | OK outcome without a world sha leaves the task in WORLD_BUILDING forever | reject INVALID | -18 | assigned |
| R6 | cost | handoffs.after_world | first authoring failure (timeout, gateway error) rejects the task | world-retry<k> with failure_kind infra/author | -18 | assigned |
| R7 | cost | models.DOCKER_JOB_TYPES | hour-long authoring jobs can hold every Docker slot; one-shot drafts in agent mode take a slot | SDE_AGENT_SLOTS reserve; per-job `needs_docker` | -18 | assigned |
| R8 | cost | worker.py world_fn/draft_fn | authoring trace saved only on success; a retry re-pays | save_trace in finally; harness adopts finished trials | -18 | harness adopts (272af87); finally assigned |
| R9 | bug, cost | authoring cost | the gateway's glm-5.1 usage.cost is a flat 5e-05 placeholder | cost from tokens × price; gateway figure kept for audit | -18 | **done** in harness 272af87 |
| R10 | bug | shared/taskformat/world_check.py | `-v` bind mounts break through the Docker socket; image tags collide across concurrent checks | derived check image; unique tags | -18 | **merged** (TB1) + tags in 272af87 |
| R11 | eff, cost | authoring containers | containers/images the agent starts via the socket outlive the trial; uncapped | label by job, clean up in finally; --cpus 1 --memory 2g | -18 | assigned |

## New in this review

| # | Sev | Where | Problem | Proposed fix | Owner |
|---|---|---|---|---|---|
| N1 | **bug** | shared/jobs/reconciler.py `reconcile` | A dead-lettered `world` or `world_check` job doesn't reject its task: the reject branch checks `(*GENERATION_TYPES, VALIDATE)` only, so the task stays in WORLD_BUILDING / WORLD_CHECKING forever and its batch never finishes | use `(*AGENT_GENERATION_TYPES, *GATE_TYPES)`; test a dead world job → task REJECTED infra_unresolved | -c0 (with INT) |
| N2 | bug (report) | evaluation/verdict.py `report`, api/service.py version view | A version's origin/operators/mutations are read from its first create/revise job. In agent mode a mutated version starts with a `world` job (origin "mutation", mutations), and its create job says origin "initial", so every mutated version renders as "initial" with no operators | read origin and mutations from the version's first job of type WORLD, CREATE or REVISE (excluding repairs); a test with a mutated agent-mode version | -c0 (verdict) and -75 (api/service) |
| N3 | eff | shared/jobs/jobqueue.py `claim` + sql.claim | When the machine's Docker slots are full, every poll still tries each of up to 20 Redis candidates per type: each is a transaction under the global advisory lock (Postgres) plus two lookups in `missed`. In agent mode, with hour-long slot holders, that is a constant DB churn per worker thread | count live Docker leases for the host once per poll; if full, skip candidates of Docker types (still try non-Docker ones) | -18 (after R7) |
| N4 | design | shared/jobs/jobqueue.py `_insert_followup` | follow-ups inherit the parent's `bundle_sha256`/`config_hash` columns and, when `input` is falsy (`{}`), the parent's whole input. A too-easy `world` job for v n+1 is pinned to v n's bundle; an intentionally empty input silently becomes the parent's | inherit pins only within a version (`f["version"] == parent["version"]`); use `f["input"] if "input" in f` | -18 |
| N5 | design | shared/storage/sql.py Postgres `tx` | every transaction takes one global `pg_advisory_xact_lock`: all claims, settles and handoffs across all machines are serialized. Fine for ≤ ~20 workers; it becomes the ceiling for a larger fleet | keep it for now (simple and correct); if needed later, `FOR UPDATE SKIP LOCKED` on the claim plus row locks on the task in handoffs | none (noted) |
| N6 | cost | validator/gates.py leak gate inside `validate` | the leak review is a multi-minute glm-5.1 call that holds a Docker slot while it waits | move it to the end of the create job (before validate); a leaking draft is repaired before any Docker time | -18 (backlog after TB3) |
| N7 | eff | shared/storage/artifacts.py S3 `_upload_dir` | uploads file by file, serially; an authoring trial's artifacts (world copies, build output) can be thousands of files | the harness collects only /logs/artifacts; cap its size and file count (e.g. 50 MB / 5k files) and fail the job with feedback when exceeded; upload with a small thread pool | -18 |

## Architecture notes (no change needed now)

- **One job per authoring step is right.** world, world_check, create and validate
  are separate rows, so a failed create retries without rebuilding the world, a
  world can be reused by several tasks, and each step's cost and time are visible.
- **Fencing is consistent.** heartbeat / settle / release are compare-and-set on the
  lease token, and every handoff is a CAS on (version, stage), so duplicate runs
  waste money but never corrupt state. The remaining duplicate-run risks are R1
  (fixed) and R8 (assigned).
- **Mode is per job, not per process** (R2), which makes switching SDE_AUTHOR_MODE
  safe for in-flight tasks. Keep this rule for any new job type.
- **Watch next: the cost split.** In agent mode authoring dominates spend (estimated
  $1-4 per world plus task vs about $0.06 per 5 rollouts). API1's reports split
  authoring from evaluation cost; P1 must check the real authoring cost against the
  estimate before any batch.
