# Changelog

## 2026-11-17: workers on queues; the Analysis service decides

**What changed**
- **No central orchestrator.** The API service only submits a batch (tasks plus their first
  `generate` jobs) and serves reads. Every other component is a worker on its own queue
  (`sde:generate`, `sde:validate`, `sde:rollout`, `sde:analyze`). Each worker settles its job and
  enqueues the next stage's job in the same transaction (`docs/PRD.md` §5).
- **The Analysis service decides.** Executors upload the whole trace to S3 and always enqueue an
  `analyze` ping. Once a version has five valid rollouts, the Analysis service runs the trace
  parser and one GLM-5.1 review over all five, then accepts, sends a revision or repair `generate`
  job back to `sde:generate`, or rejects. Each task-stage transition has exactly one owning service.
- **The trace review moved out of the executor.** `evaluation/trace_parser.py` (deterministic
  metrics and evidence, plus a review that must cite evidence or count as `unexplained`) is called
  by the Analysis service once per version, not per rollout inside the executor.
- **Cross-machine trace adoption.** Executors fetch a trace another machine already uploaded before
  running a reclaimed rollout, and upload their own trace the moment Harbor finishes, before
  settling. A worker killed after that point leaves a trial the next owner adopts instead of rerunning.
- **The generation worker serves three paths from one queue:** initial drafts, repairs after
  failed gates, and revisions after an out-of-band verdict, selected by `input.origin`.

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
