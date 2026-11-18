# Changelog

## 2026-11-18: version cap 8

**What changed**
- `MAX_VERSIONS` defaults to 8 (v1 plus up to 7 revisions), down from 10; `SDE_MAX_VERSIONS`
  still overrides it (the e2e suite pins 3). The PRD and the diagram say v8.

**Why**
- Eight versions leave room for several harder/easier passes (now guided by the Trace
  Parser's review of passing runs) while capping a never-in-band task at ~8× one evaluation.

**Tradeoffs**
- A task that would have reached the band at v9 or v10 is now rejected `exhausted`.

## 2026-11-18: batches are back, as a label, with a yield report

**What changed**
- `POST /v1/batches` `{count=10, batch_id?, prompt?|spec?}` creates N tasks (`<batch_id>-tNN`) in
  one transaction; `GET /v1/batches`, `GET /v1/batches/{id}` and `GET /v1/batches/{id}/report`
  list, show and report on them. Every `/v1/tasks` route and `/v1/report` is unchanged.
- `tasks.batch_id` returns as a nullable, indexed label (schema init adds it; SQLite databases
  from the first batch release, where it was NOT NULL, are rebuilt). Single tasks leave it null.
- Every report gains a `yield` block: candidates, functional, accepted, accepted_v1 and
  `learnable_rate` = accepted ÷ finished candidates (null until one finishes).
- `create_task` and `create_batch` share one insert; a batch samples each task's slot against
  every task so far plus its own earlier picks. `scripts/run_tasks.py` uses one batch
  (`--batch`, `--count`, `--prompt`), resumes on rerun and prints the yield.

**Why**
- A batch's yield is the number that gets reported. Ten loose tasks plus a list of ids worked, but the
  batch is the unit we report on, so it should be one call to create and one id to report.

**Tradeoffs**
- Still no batches table and nothing downstream knows about batches: the label only groups
  tasks for status and reporting. Idempotency compares the count (and a shared prompt/spec);
  a sampled batch can't be compared slot by slot, since sampling isn't deterministic.

## 2026-11-18: one task per request; no batches

**What changed**
- `POST /v1/batches` and every batch route are gone. `POST /v1/tasks` creates one task from a
  free-text `prompt` (it seeds the standard authoring prompt), an explicit `spec` (taxonomy,
  difficulty, design lines), or nothing (the service samples the least-used taxonomy slot across
  all tasks). An optional `task_id` makes retries safe; reusing it for a different request is 409.
- `GET /v1/tasks` lists tasks; `GET /v1/report` reports over named task ids or everything since a
  time. `tasks.batch_id` is dropped (schema init migrates old databases) along with the queue's
  batch filter, `--batch` flags and the batch store helpers.
- `scripts/run_tasks.py` is the batch run: post N tasks, wait, report on those ids.

**Why**
- The system never needed to group work: every service handles one task's jobs. Batches only
  shaped the API and the report, and pulled sampling, idempotency and status into a concept that
  had no behaviour of its own. One task per request is simpler, and a batch of 10
  is just ten requests whose ids the report is given.

**Tradeoffs**
- Diversity sampling now looks at every task ever created rather than one batch, so it spreads
  across runs too. A report needs the caller's task ids (or a time window) instead of a batch id.
- Dropping a column breaks still-running old code: stop every process before starting the new
  release (no rolling deploy across this change).

## 2026-11-17: services on queues; Generation Service and Trace Parser own the loop

**What changed**
- **No central orchestrator; `job_service/` is gone.** The API service only creates tasks
  (one task plus its `create` job per call) and serves reads. Every other component is a service on its
  own queue: a **create** queue for new tasks, a **revise** queue for repairs and revisions, a
  validate queue, the **run** queue (`sde:rollout`) and an `sde:analyze` queue. Each service settles
  its job and enqueues the next stage's job in the same transaction (`docs/PRD.md` §5).
- **Generation Service:** consumes the create and revise queues (one service, revise first),
  drafts or changes a task with GLM-5.1, then hands off to Build + Validate.
- **Trace Parser decides.** Executors upload the whole trace to S3 and always enqueue an
  `analyze` ping. Once a version has five valid runs, the Trace Parser (its own service, not a
  cron batch) extracts evidence and has GLM-5.1 review each failed run, then accepts, puts a
  revision or repair on the revise queue, or rejects. Each task-stage transition has exactly one
  owning service.
- **The trace review moved out of the executor.** `evaluation/trace_parser.py` (deterministic
  metrics and evidence, plus a review that must cite evidence or count as `unexplained`) runs in
  the Trace Parser service once per version, not per rollout inside the executor.
- **Cross-machine trace adoption.** Executors fetch a trace another machine already uploaded before
  running a reclaimed rollout, and upload their own trace the moment Harbor finishes, before
  settling. A worker killed after that point leaves a trial the next owner adopts instead of rerunning.
- **Job type `generate` split into `create` and `revise`,** so new drafts and changes to existing
  tasks have their own queues (and revisions drain first). Both run in the Generation Service;
  `input.origin` (`initial` / `repair` / `revision`) still selects the prompt.

**Why**
- An orchestrator that routed every stage back through one service made it look responsible for
  everything and put slow LLM calls in its decision loop. Direct hand-offs keep each service to one
  job, and they can't be lost, because the hand-off commits with the result.
- Reviewing all five rollouts of a version together is cheaper (one GLM call, not one per failure),
  frees Docker slots sooner, and lets the review compare failures with passes, which is what the
  revision prompt needs.

**Trade-offs**
- The flow rules are spread across the workers instead of living in one file. The stage-ownership
  table in `docs/PRD.md` §5.1 is the single place that lists them.
- Every settled rollout enqueues an `analyze` ping, and only the one that completes the version
  does any work. The rest finish immediately; that's the cost of not needing a central counter.
