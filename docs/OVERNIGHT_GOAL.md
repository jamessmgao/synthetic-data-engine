# Overnight goal

I'm going to sleep. Run an iterative tuning loop all night on the production K3s cluster
(AWS), following docs/ITERATION_RUNBOOK.md.

Goal: novel, diverse tasks that the pinned solver (terminus-2 + glm-5.3-flash) passes 1-3 out
of 5 times.

We're optimizing two things together:
- the task GENERATION (prompts first, implementation only when needed);
- the REVISION pipeline (how a task that's out of band becomes a better version).

Branch: work on main (currently 0609e8d: the K3s production setup + tune-r4 prompts; the same
code as infra-authoring). Commit, push and deploy only from it. Never deploy from any other
branch.

Iteration 0: batch aws-agent-b1 (10 tasks) is already running. Do NOT cancel it. Watch it
under the stopping rules below, log it as iteration 0, then deploy main and start
iteration 1.

Each iteration:
1. Submit a batch of 6 tasks, one per family, budget_usd 40, via the API (docs/API.md;
   `make aws-k8s-public` prints the current URL, which changes on every deploy).
2. Watch it end to end:
   - world authoring, world check, task authoring, validate, the 5 rollouts, the Trace
     Parser's analysis;
   - ESPECIALLY the solver trajectories (S3 traces/<job>/);
   - every revision (repair, world retry, world mutation, revise).
3. Stopping rules, per task, as it runs:
   - In band, or progressing normally, or in a revision round that's producing useful signal:
     let it run, even if long.
   - One authoring stage (world or task) over ~45 min: inspect its trajectory.
     - If it's stuck (no new agent steps for ~15 min, the same commands or errors repeating,
       waiting on a dead process), cancel it with a note.
     - If it's slow but making real progress, let it run.
   - ~1.5 h without a first verdict: cancel it, unless it's clearly still informative.
   - The same infrastructure error twice in a row: cancel it and fix the infrastructure in git.
4. Batch end = every task decided or cancelled (hard cap ~3 h). Then:
   - for EACH task, record one finding (where it ended and why) and make ONE small, targeted
     change addressing it;
   - skip a task's change if its lesson is unclear;
   - never make two changes to the same prompt section in one batch;
   - write down what you expect each change to do.
5. Log the iteration in docs/ITERATION_LOG.md:
   - batch id and commit sha;
   - per task: the family, stage reached, pass count per version, cost, time, the finding,
     and the change it led to;
   - the in-band rate per family and in total, and the revision metrics below.
6. Run the unit tests locally, commit, push, `make aws-k8s-deploy OVERLAY=aws-prod`, check
   /healthz, start the next batch. Deploy ONLY between batches: a deploy restarts pods, and a
   killed authoring session is paid for twice.

Optimizing the hill climb itself:
- The metric: the in-band rate (1-3/5), per family and in total. Keep the batch's changes if it
  doesn't drop. If two consecutive batches get worse, revert, riskiest change first.
- Confirm before trusting: confirm a change that looks good on one wider batch of 10-12 before
  building on it; one batch of 6 can be luck.
- Fine tweaks only: never sudden or drastic rewrites.
- If tasks are far too hard (mostly 0/5), start easier (dial 1) and climb. If they're too easy
  (mostly 5/5), raise the difficulty.
- Diversity: spread mechanisms within each family, and never repeat a scenario.

Optimizing the revision pipeline (the algorithm, not only the prompts):
- For every revision, measure: did version N+1 move TOWARD the band compared with version N
  (pass count closer to 1-3)? Track in the log, per family:
  - how many revisions it took to land in band;
  - revisions that overshot (too easy to too hard, or back);
  - revisions that failed outright (repair or retry exhausted).
- The revision path (world mutation / revise from the Trace Parser's analysis) hasn't been
  confirmed end to end on this cluster yet. Watch the FIRST one closely: it must fetch the
  previous version's draft and world from S3 (possibly authored on another node) and produce a
  real new version. If it breaks, that's an infrastructure fix, top priority.
- Tune what the analysis feeds into a revision: if revisions don't converge, improve how the
  failure analysis turns into the mutation instruction (what was too easy, what to change), not
  only the base prompts.
- World failures and retries count too: if worlds keep failing the same check, fix the cause
  (prompt or self-check), because a failed world wastes the whole task.

Limits:
- Stay within the $220 total budget (/healthz shows the spend). Never raise any cap.
- Never set a short solver timeout: timeouts don't count as the model failing.

Infrastructure:
- Monitor the cluster (make aws-k8s-status, make aws-k8s-logs SVC=..., /healthz). Watch for pod
  restarts, KEDA scaling the executor under rollouts, RDS connections (keep under ~70), and
  CPU/memory.
- If something breaks:
  - fix it in git, then commit, push and redeploy (after the batch, or cancel the batch first
    if the break blocks it), and restart the batch;
  - live inspection is fine; live editing of files in pods isn't.
- Never: Terraform, `make aws-k8s-down`, deleting RDS/S3 data, changing instance or
  security-group settings, toggling mock mode, or scaling generation or executor above 10 (RDS
  connection limit).
- If a deploy fails on expired AWS credentials (ExpiredToken), stop deploying, keep monitoring
  and tuning locally, and note it in the log. The cluster and the API keep running.
- You are the only one changing prompts tonight.

## Your team: three agent sessions

You have three agent sessions at your disposal. You are their manager: delegate to them, and
keep the decisions and the branch yourself.

- **Delegate the watching and the analysis:**
  - following tasks through their stages;
  - reading solver trajectories and Trace Parser analyses;
  - diagnosing stuck or failed jobs;
  - drafting a proposed change per task, with the evidence behind it;
  - investigating an infrastructure failure.

  A good split: one session per two families, or one on trajectories, one on revisions, one on
  the cluster.
- **Keep for yourself:**
  - the decision on each change;
  - all edits to prompts and the revision logic (the rule above stays: you are the only one
    changing prompts);
  - commits to main;
  - deploys;
  - batch submission and cancellation;
  - the iteration log.
- **How they work:**
  - each session has its own worktree and branch;
  - they propose changes as diffs or branches for you to review, merge into main or
    reject;
  - they never push to main, deploy, cancel batches, or change budgets or cluster
    settings themselves;
  - give each one a clear brief (what to watch, what to report, and when), and have them
    report back to you with evidence (job ids, trajectory excerpts, pass counts).
- **Every limit and "Never" above applies to them too.**

## Done when

The task creation yield is at least 80% on one batch of 10 or more: at least 80% of the tasks
submitted are ACCEPTED in band (1-3/5) because they are genuinely difficult. Not counted as
accepted: tasks rejected or cancelled for malformation, failed validation, broken worlds,
infrastructure errors or timeouts. Record that batch's per-task evidence in
docs/ITERATION_LOG.md. If the $220 budget runs out first, stop and write a final summary there
instead.
