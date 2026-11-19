# Hill climbing: how we tune task generation on the production cluster

This doc merges three earlier docs (2026-11-19):
- HILLCLIMBING.md, the method;
- HILLCLIMBING.md, the goal statement;
- HILLCLIMBING.md, the batch-era runbook.

What happened in each iteration and window is in [ITERATION_LOG.md](ITERATION_LOG.md).

## 1. The goal

**Novel, diverse tasks that the pinned solver passes 1–3 out of 5 times.** The pinned solver is
terminus-2 + glm-5.3-flash at reasoning high.

**What we optimize:**
- task generation: prompts first, implementation only when needed;
- the revision pipeline: how an out-of-band task becomes a better version.

**Done when:** at least 80% of the tasks in a window of 10 or more are ACCEPTED in band, because
they are genuinely difficult. That window's per-task evidence goes in the log.

These don't count as accepted:
- tasks rejected or cancelled for malformation, failed validation or a broken world;
- tasks lost to infrastructure errors or timeouts.

If both gateway keys run out first, stop and write a final summary in the log instead.

The goal as the user set it (2026-11-18), verbatim:

> Run the hill-climbing loop on the production K3s cluster (AWS) … a sliding window of continuous
> tasks, run as six lanes (one per agent session, each with ~1/6 of the cluster's capacity, its own
> strategy and lever, watching its own tasks and proposing tweaks on its own branch); you are the
> manager: you merge the lanes' tweaks into main and deploy them periodically with drains as their
> mini-batches complete; each change is judged on the tasks authored after it and logged in
> docs/ITERATION_LOG.md. … Goal: novel, diverse tasks the pinned solver (terminus-2 +
> glm-5.3-flash) passes 1-3 out of 5 times; optimize task generation and the revision pipeline.
> Branch: main only. Hard limits: Terraform or node/pod-count changes only with my explicit OK;
> never aws-k8s-down, delete RDS/S3 data, change security groups or toggle mock mode; never set a
> short solver timeout. Done when at least 80% of the tasks in a window of 10+ are ACCEPTED in band
> because they are genuinely difficult; record that evidence in the log.

## 2. The sliding window

Iterations 0–2 ran as whole batches: submit, wait for every task to be decided, then change and
deploy. A task takes hours end to end: a world 20–45 min, authoring 10–20 min, 5 agent runs, then
revisions. So from 2026-11-18 the loop runs as a sliding window:

1. **Keep the pipeline fed.**
   - Submit tasks continuously, so authoring and agent runs always have work.
   - A *window* is the set of tasks authored under one version of the pipeline.
2. **One targeted tweak per clear finding.** A finding comes from a trajectory, a Trace Parser
   analysis, a leak review or a failed world. The tweak goes to one lever: a prompt section, a
   family brief, a revision rule, a calibration threshold, a self-check, or a deployment
   setting. Write down the expected effect.
3. **Deploy right away, drained.**
   - Generation and executor deploys are rolling updates with drain: old pods stop claiming,
     finish their running authoring sessions or agent runs (up to ~60 min), then exit. No work is
     lost.
   - Scoped deploys (`make aws-k8s-deploy OVERLAY=aws-prod ONLY=…`) touch only what changed.
4. **Every job records the release it ran under**, so each change is measured on the tasks
   authored after it.
5. **Judge each change on its window.**
   - Keep a change if the window's yield doesn't drop.
   - Revert it if the two windows after it get worse, riskiest change first.
   - Confirm a good-looking change on 10+ tasks before building on it.
6. **Don't stack changes on one lever.** Never make two changes to the same prompt section or rule
   inside one window.

## 3. The team: one manager, lanes

Each agent session runs a **lane**: a set of families or a lever (world design, instructions and
tests, mutations and calibration, family briefs, the harness, the sampler). A lane:
- submits its own mini-batches (`aws-w<window>-<lane>…`) and keeps about 5–6 tasks in flight;
- watches its own tasks end to end;
- turns findings into one-change branches, with the evidence and the expected effect.

Lanes never push to main, deploy, cancel other lanes' tasks, or change budgets or cluster
settings, unless the user asks them directly.

**The manager:**
- assigns the lanes and keeps capacity split evenly;
- merges the lanes' tweaks into main (one change per lever per window), runs the tests and
  deploys;
- keeps the iteration log.

## 4. Watching a task

Watch every stage:
- world authoring, world_check, task authoring, the leak review, validate;
- the 5 agent runs, especially the **solver trajectories** (S3 `traces/<job>/`);
- the Trace Parser's analysis;
- every revision (repair, world repair, world mutation or variant, easier).

**Stopping rules:**

| Situation | Action |
|---|---|
| progressing normally, already in band, or in a revision round that gives useful signal | let it run |
| one authoring stage over ~45 min | inspect its trajectory |
| ↳ stuck: no new agent steps for ~15 min, the same commands or errors repeating, waiting on a dead process | cancel it (`POST /v1/tasks/<id>/cancel` with a `note`) and log why |
| ↳ slow but making real progress | let it run |
| ~1.5 h without a first verdict | cancel, unless it's clearly still informative |
| the same infrastructure error twice in a row | cancel, then fix the infrastructure in git |

**Take every failure the Trace Parser reports as legitimate** (the user's rule, 2026-11-18):
report its attribution as given, and don't fix or second-guess it. A flaw seen in a task becomes
a task-generation lesson. A timed-out agent run goes to the Trace Parser:
- ruled a model failure (wedged its own terminal, looping, abandoned the task): it counts as a
  fail;
- ruled not the model's fault (still making progress, slow infrastructure): it's INVALID and
  retried.

## 5. Tuning the climb, and the revisions

**The metric:** the in-band rate (1–3/5), per family and in total.

**Tuning the climb:**
- Make fine tweaks, never drastic rewrites.
- Mostly 0/5: start easier (dial 1) and climb. Mostly 5/5: raise the difficulty.
- Diversity: spread mechanisms within each family, and never repeat a scenario.

**Tuning the revisions:**
- For every revision, record whether version N+1 moved toward the band. Track, per family:
  revisions to the band, overshoots, and outright failures (repair or retry exhausted, leak
  review).
- If revisions don't converge, improve how the failure analysis becomes the mutation instruction,
  not only the base prompts.
- A failed world wastes the whole task, so fix the cause of recurring world failures.

The dashboard's [/analytics](../web/README.md) page shows these per family, mechanism and revision
origin.

## 6. World sourcing: the rules over the night of 2026-11-18

| From (UTC) | Rule (the user's) |
|---|---|
| until ~21:20 | every task builds a new world |
| ~21:20 | **build no new worlds.** New tasks reuse checked worlds (`POST /v1/batches` with `"reuse_worlds": true`, `GET /v1/worlds` lists the pool); keep world mutations; never cancel running world jobs |
| ~23:25 | distributed-systems injection: new distributed worlds allowed if reuse can't supply one |
| ~00:00 | "stop using old worlds": new worlds only, on the new taxonomy, at dial 3, mostly `distributed_concurrency` |

Reuse picks:
- least-reused first;
- prefer worlds whose earlier task was rejected or too easy;
- skip leaked and greedy-defect worlds.

Variants plant a new mechanism's bug in an existing world lineage.

## 7. Limits

- **Budget:** no total cap (the user removed it on 2026-11-18); be reasonably frugal (`/healthz`).
  When the gateway key runs out ("API key spend limit exceeded"), switch to the backup key. It's
  outside the repo, and its file says how. Never print either key.
- **Timeouts:** never shorten the solver timeout (1 hour).
- **Changes go through git:** commit, push and deploy. Live inspection is fine; live editing of
  files in pods isn't.
- **Only with the user's explicit OK** (show the plan first): Terraform (instances, RDS class), and
  changing node or pod counts.
- **Never:** `make aws-k8s-down`, deleting RDS or S3 data, security-group changes, or toggling mock
  mode.
- **If a deploy fails on expired AWS credentials** (`ExpiredToken`): stop deploying, keep
  monitoring, and note it in the log. The cluster and the API keep running.

## 8. The cluster and monitoring

Sizing as of 2026-11-19; [CAPACITY.md](CAPACITY.md) has the measurements:
- 17 EC2 instances (9 on-demand, 8 Spot), RDS db.t4g.medium (~400 connections);
- generation: 14 pods × 3 authoring sessions;
- executors: 2–16 pods (KEDA) × 2 agent runs, a 500m CPU request per pod;
- validate: 4 pods.

Spot instances are drained on their interruption notice, and a worker hands its jobs back at once.

```bash
make aws-k8s-status                  # nodes, pods, services
make aws-k8s-logs SVC=generation     # or api | control | validate | executor
curl -s $SDE_URL/healthz              # status, DB, spend   (make aws-k8s-public prints SDE_URL)
```

Watch pod restarts, KEDA scaling, RDS connections, CPU (each agent run is ~1 vCPU) and memory. If
something breaks: read the logs, fix it in git, redeploy.

## 9. Logging

In [ITERATION_LOG.md](ITERATION_LOG.md), per window:
- the change (what, why, the expected effect), its commit and when it went live;
- per task: the family, the stage reached, passes per version, cost, time and the finding;
- the in-band rate per family and in total, and the revision metrics;
- whether each change was kept or reverted.

Tasks with no recorded cost, and batches in `SDE_DEV_BATCHES`, are dev: they're left out of the
Prod numbers and of the yield.
