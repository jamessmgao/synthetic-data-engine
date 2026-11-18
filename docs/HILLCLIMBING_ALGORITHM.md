# Hill-climbing algorithm

An iterative tuning loop on the production K3s cluster (AWS), following
docs/ITERATION_RUNBOOK.md.

Goal: novel, diverse tasks that the pinned solver (terminus-2 + glm-5.3-flash) passes 1-3 out
of 5 times.

We're optimizing two things together:
- the task GENERATION (prompts first, implementation only when needed);
- the REVISION pipeline (how a task that's out of band becomes a better version).

Branch: work on main. Commit, push and deploy only from it. Never deploy from any other branch.

History: iterations 0-2 ran as whole batches (submit, wait for every task to be decided, then
change and deploy). From 2026-11-18 the loop runs as a sliding window, below.

## The sliding window

A task takes hours end to end (a world 20-45 min, authoring 10-20 min, 5 agent runs, then
revisions), so waiting for a whole batch to finish before changing anything wastes most of the
night. Instead, keep a steady stream of tasks in flight and change the pipeline as findings
arrive:

1. **Keep the pipeline fed.** Submit tasks continuously (one per family per wave, novel
   scenarios), so authoring and agent runs always have work. A "window" is the set of tasks
   authored under one version of the pipeline.
2. **Apply targeted tweaks as you go.** When a finding is clear (from a trajectory, a Trace
   Parser analysis, a leak review, a failed world), make ONE small, targeted change to a lever of
   task creation: a prompt section, a family brief, a revision rule, a calibration threshold, a
   self-check, or a deployment setting. Write down what you expect it to do.
3. **Deploy it right away.** A generation deploy is a rolling update with drain: each old pod
   stops claiming, finishes its running sessions (up to ~60 min), then exits, while new pods take
   new jobs, so no session is killed. Scoped deploys (`make aws-k8s-deploy OVERLAY=aws-prod
   ONLY=...`) touch only the components that changed. Once prompt hot-reload is built, prompt
   changes need no deploy at all.
4. **Every job records the version it ran under** (the release sha, and the prompt-set hash once
   tagging is built), so each change is measured on the window of tasks authored after it, not on
   a whole batch.
5. **Judge each change on its window.** Compare the in-band rate (and the revision metrics) of
   the tasks authored under the new version with those under the previous one. Keep a change if
   the window's yield doesn't drop; revert it if two consecutive windows after it get worse,
   riskiest change first. Confirm a change that looks good on 10+ tasks before building on it.
6. **Don't stack changes on one lever.** Never two changes to the same prompt section (or the same
   rule) inside one window: the window must be able to say which change did what.

Optional: a canary (a small `generation-canary` Deployment, or some pods reading prompt set B)
runs a change on part of the traffic, for an A/B comparison over the same period.

## What to watch, per task

- world authoring, world check, task authoring, the leak review, validate, the 5 agent runs, the
  Trace Parser's analysis;
- ESPECIALLY the solver trajectories (S3 traces/<job>/);
- every revision (repair, world retry, world mutation, revise).

Stopping rules, per task:
- In band, or progressing normally, or in a revision round that's producing useful signal: let it
  run, even if long.
- One authoring stage (world or task) over ~45 min: inspect its trajectory. If it's stuck (no new
  agent steps for ~15 min, the same commands or errors repeating, waiting on a dead process),
  cancel it with a note; if it's slow but making real progress, let it run.
- ~1.5 h without a first verdict: cancel it, unless it's clearly still informative.
- The same infrastructure error twice in a row: cancel it and fix the infrastructure in git.

Take every failure the Trace Parser reports as legitimate (the user's rule, 2026-11-18): report
its attribution as given, and don't fix or second-guess the Trace Parser. A flaw seen in a task
becomes a task-generation lesson.

## Logging

Log in docs/ITERATION_LOG.md, per window:
- the change (what, why, the expected effect), its commit sha and when it was deployed;
- per task authored in the window: the family, stage reached, pass count per version, cost,
  time, the finding;
- the in-band rate per family and in total, and the revision metrics below;
- whether the change was kept or reverted.

Tasks with no recorded cost, and batches listed in SDE_DEV_BATCHES, are dev: they're left out of
the dashboard's Prod numbers and of the yield.

## Optimizing the hill climb itself

- The metric: the in-band rate (1-3/5), per family and in total.
- Fine tweaks only: never sudden or drastic rewrites.
- If tasks are far too hard (mostly 0/5), start easier (dial 1) and climb. If they're too easy
  (mostly 5/5), raise the difficulty.
- Diversity: spread mechanisms within each family, and never repeat a scenario.

## Optimizing the revision pipeline (the algorithm, not only the prompts)

- For every revision, measure: did version N+1 move TOWARD the band compared with version N
  (pass count closer to 1-3)? Track, per family:
  - how many revisions it took to land in band;
  - revisions that overshot (too easy to too hard, or back);
  - revisions that failed outright (repair or retry exhausted, or the leak review).
- Tune what the analysis feeds into a revision: if revisions don't converge, improve how the
  failure analysis turns into the mutation instruction (what was too easy, what to change), not
  only the base prompts.
- World failures and retries count too: if worlds keep failing the same check, fix the cause
  (prompt or self-check), because a failed world wastes the whole task.

## Limits

- No budget cap (the user removed it on 2026-11-18): be reasonably frugal (/healthz shows the
  spend). When the gateway key runs out ("API key spend limit exceeded"), switch to the backup key
  (outside the repo; its file says how).
- Never set a short solver timeout: timeouts don't count as the model failing.

## Infrastructure

- The cluster (2026-11-18): 17 EC2 instances (9 on-demand, 8 Spot), RDS db.t4g.medium (~400
  connections); generation 11 pods x 3 authoring sessions; executors 2-10 pods (KEDA) x 3 agent
  runs; validate 4. Spot nodes are drained on their interruption notice (the termination
  handler), and a worker hands its jobs back at once.
- Monitor the cluster (make aws-k8s-status, make aws-k8s-logs SVC=..., /healthz): pod restarts,
  KEDA scaling, RDS connections, CPU (agent runs are CPU-bound: ~1 vCPU each) and memory.
- If something breaks: fix it in git, then commit, push and redeploy. Live inspection is fine;
  live editing of files in pods isn't.
- Only with the user's explicit OK for that change (show the plan first): Terraform (instances,
  RDS class), changing node or pod counts beyond the sizing above.
- Never: `make aws-k8s-down`, deleting RDS/S3 data, security-group changes, toggling mock mode.
- If a deploy fails on expired AWS credentials (ExpiredToken), stop deploying, keep monitoring and
  tuning locally, and note it in the log. The cluster and the API keep running.

## Your team: agent sessions

You manage the other agent sessions: delegate to them, and keep the decisions and the branch
yourself.

- **Delegate the watching and the analysis:** following tasks through their stages; reading
  solver trajectories and Trace Parser analyses; diagnosing stuck or failed jobs; drafting a
  proposed change per finding, with the evidence behind it; infrastructure work on branches.
- **Keep for yourself:** the decision on each change; all edits to prompts and the revision logic;
  commits to main; deploys; task submission and cancellation; the iteration log.
- **How they work:** each session has its own worktree and branch; they propose changes as
  branches for you to review, merge into main or reject; they never push to main, deploy, cancel
  tasks, or change budgets or cluster settings themselves unless the user asks them directly;
  they report back with evidence (job ids, trajectory excerpts, pass counts).
- **Every limit above applies to them too.**

## Done when

The task creation yield is at least 80% over a window of 10 or more tasks: at least 80% of the
tasks submitted in it are ACCEPTED in band (1-3/5) because they are genuinely difficult. Not
counted as accepted: tasks rejected or cancelled for malformation, failed validation, broken
worlds, infrastructure errors or timeouts. Record that window's per-task evidence in
docs/ITERATION_LOG.md. If both gateway keys run out first, stop and write a final summary there
instead.
