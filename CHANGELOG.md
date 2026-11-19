# Changelog

## 2026-11-18: agent-mode, world-first authoring (plan v2), end to end

The plan is docs/AUTHORING.md; the method is docs/WORLD_CONSTRUCTION.md.
The review findings (R*, N*, L*) and their resolutions are in docs/ARCHITECTURE_REVIEW.md.
Each item lists its main commits.

**What changed**

- **Job types and hand-offs (INF1, 626b4ef).**
  - New `world` and `world_check` jobs, and `create` from a world;
    `after_world` → `after_world_check` → `after_generation` → `after_validate`.
  - World artifacts are content-addressed (`worlds/<sha>/`) in the artifact store, next
    to bundles, drafts and traces.
  - `SDE_AUTHOR_MODE=oneshot|agent`. Agent jobs hold a Docker slot and an hour-long lease.
- **Trace Parser world mutation (EX1, e489e6b, de5d3c3, bebf27d).**
  - Too easy → a `world` mutation job from the parent world; too hard → `revise`
    `easier`; task blamed → `revise` `repair_ambiguity`.
  - Operators come from deterministic rules (`calibration.choose_mutations`; rule names in
    docs/EVALUATION.md); `remove_cues` is always first when the pass review finds cues.
  - Every choice records `{operator, rule, evidence}`. The history travels in job inputs.
- **Gates (TB1, 3601f5c; f5b0175; c369b95).**
  - `validate` gains `wrong_fix` (the bundle's plausible wrong fix must score 0) before
    `leak`.
  - `world_check` builds the world: repro fails twice, repro passes with the fix twice,
    the symptom is visible, the fix isn't one local patch (2+ files, or a dependent the
    wrong fix breaks), and the wrong fix clears the symptom but fails the repro.
  - `SDE_WORLD_HARDNESS = off | warn (default) | enforce | strict`; `result.hardness`
    records the hardness half.
- **Authoring kit (TB2, 272af87, 977d7b1, 7cd990f, 780af13, 87d39e8, c9882a9, 6d231bb).**
  - The `sde-authoring` image, the `sde-world` / `sde-task` self-check CLIs, world scaffolds,
    30/20-min time boxes, `run_world_job`.
  - Fixed: Harbor 0.26 puts `/logs/artifacts` under the trial's `artifacts/` (6d231bb).
- **Task author (TB3, f257ca3, 5c9364a, 0376e02, d6ab4b3, 7eefd40, 4baa6ac).**
  - `run_create_job` from a world.
  - The leak review moved into `create`; a leaking draft is repaired, not rejected (N6).
  - The instruction no longer describes the counterexample schedule, the fault-hook
    position or the fix's shape.
- **Tuning (f8c9081, 152b0b6, a124f4a).**
  - Harder worlds and tasks: non-local fixes, external faults, invariant property tests.
  - A semantic and blind leak review (`SDE_LEAK_BLIND`).
  - Every world invariant has a marked test; randomized tests use fixed seeds; the oracle
    runs twice (flake check).
- **Taxonomy v2 and API (API1, e572f4a).**
  - Six families × mechanisms × environments; spec v2 (family, mechanism, shape, dial).
  - `GET /v1/families`; `area` / `mechanism` / `environment` / `dial` on tasks and
    batches; reports by family and mechanism.
- **Per-job dispatch (135b82b).** A create or revise with `world_sha256` runs the agent
  author; anything else runs one-shot, even in agent mode. Repairs keep their world
  through validate.
- **Spend cap (fcfe4c8, 5256d6d).** `SDE_BUDGET_USD` and a per-batch `budget_usd` stop new
  paid claims, using per-job estimates: world $1, create $4, revise $3.
- **Cancel (d8ca8dc).** `POST /v1/tasks/{id}/cancel` and `/v1/batches/{id}/cancel`, a
  `CANCELLED` status that doesn't count as a failure, and the cancelled job's cost kept;
  `scripts/relabel_cancelled.py` relabels earlier stops.
- **Capacity and robustness on AWS and locally.**
  - Per-job Docker slots and `SDE_AGENT_SLOTS` (f8f6e90), and generation on every runner
    (4c7be83).
  - Slot ceilings (d40c9e6, 25d4970), a memory guard, a CPU guard (f743fd1, b31ff23) and
    claim pacing (8a966c8).
  - A bounded Postgres pool, with heartbeats on their own pool (1d5d869, 5f86caa), and
    db.t4g.small (5547aeb).
  - Parallel S3 uploads (a796fbd).
- **Load-test mode (2e0612c, 25d4970, 53b8a48).** `SDE_MOCK_LLM` runs the real code with
  faked model calls, plus a step driver. It's off by default and never set for a real
  batch.
- **Tests (INT, e2fbfc0, 6272997, 884016c).**
  - Agent-mode e2e scenarios.
  - A real-adapter test across two machines on S3.
  - The cancel and authoring suites (docs/TESTING.md).
- **Docs.** README "The pipeline as built"; docs/EVALUATION.md; docs/archive/ANALYSIS.md §11–12;
  docs/TESTING.md agent mode; docs/DEPLOY.md.

**Fixes found by review or by the live runs**

- Heartbeat errors killed the heartbeat thread, so jobs were run twice (de5d3c3 worker,
  60e561b executor).
- RDS ran out of connections (1d5d869, 5547aeb).
- Suspect starvation: an abandoned job waited for hours on a busy machine (f743fd1).
- The runner image has no `ps`, so the liveness check failed and a rollout went INVALID
  (2161445).
- A dead `world` / `world_check` job didn't reject its task (N1, 02add1e).
- Mutated versions were reported as "initial" (N2, 02add1e).
- A raising authoring function lost its trial (R8, df3929e).
- A failed world job wasn't retried, and "OK" without a world now rejects (R5, R6,
  5525698).
- Follow-ups inherited pins across versions (N4, d436107).
- `review_passes` could raise.
- `compose.dev` shadowed `deploy/local.env` (12d890d).
- `test_cancel` never published its jobs, so it failed only on Redis (94038e1).

**Reverted:** "World mutations use the parent world's hardness checks" (9156191), reverted
in d0130b4, pending the tiebreak version: the user chose tiebreak-only. It's kept on the
`park/hardness-tiebreak` branch (docs/TODO.md).

**Why**
- One-shot drafts were too easy: 58 of 62 scored live runs passed, with none failing
  (docs/archive/ANALYSIS.md §11). The first world-first task (P1) produced a genuine model
  failure (§12.2).

**Tradeoffs**
- An agent-authored task costs about $2.9 against about $0.3 one-shot, and agent sessions
  hold Docker slots for up to 30 min.
- With the hardness gate at `warn`, a world can still be local; that's recorded, not
  enforced.

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
