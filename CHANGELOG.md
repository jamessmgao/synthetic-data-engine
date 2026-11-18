# Changelog

## 2026-11-17: services on queues; Generation Service and Trace Parser own the loop

**What changed**
- **No central orchestrator; `job_service/` is gone.** The API service only submits a batch
  (tasks plus one `create` job each) and serves reads. Every other component is a service on its
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
