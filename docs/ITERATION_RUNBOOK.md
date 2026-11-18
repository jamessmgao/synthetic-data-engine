# Overnight tuning runbook (production K3s on AWS)

For an agent that tunes task generation unattended. **The goal:** novel, diverse tasks that the
pinned solver (terminus-2 + glm-5.3-flash) passes **1–3 out of 5** times. **The main lever:**
prompts. Implementation changes only when needed.

The cluster: [DEPLOY.md](DEPLOY.md) §8.6. The API from the command line: [API.md](API.md).

## The loop

1. **Submit a batch:** 6 tasks, one per family, with `budget_usd: 40`. If a change targets
   one family, use 4–6 tasks of that family instead.
   ```bash
   sde -X POST $SDE_URL/v1/batches -d '{"batch_id": "tune-<n>", "count": 6, "dial": 2, "budget_usd": 40}'
   ```
2. **Watch it end to end:**
   - world and task authoring;
   - validate;
   - the 5 rollouts;
   - the Trace Parser's analysis;
   - **especially the solver trajectories** (S3 `traces/<job>/`, and each job's `result` via
     `GET /v1/tasks/<id>`).
3. **Apply the stopping rules** (below) as tasks progress.
4. **At batch end**, when every task is decided or cancelled (hard cap: about 3 h):
   - For each task, record one finding: where it ended and why.
   - Make **one small, targeted change per task**, addressing its finding. Skip a task's
     change when its lesson is unclear.
   - Never make two changes to the same prompt section in one batch.
   - Write down what you expect each change to do.
5. **Log the iteration** in `docs/ITERATION_LOG.md`:
   - the batch id and commit sha;
   - per task: family, stage reached, pass count, cost, time, the finding, and the change it led to;
   - the in-band rate, per family and in total.
6. **Ship the changes:**
   1. Run the unit tests locally (`pytest -q generator validator authoring tests/shared`).
   2. Commit and push.
   3. `make aws-k8s-deploy OVERLAY=aws-prod`.
   4. `curl $SDE_URL/healthz` (`make aws-k8s-public` prints the current URL).
   5. Start the next batch.

   Deploy **only between batches**: a deploy restarts the pods, and a killed authoring session
   is paid for twice.

## Stopping rules (per task, while the batch runs)

| Situation | Action |
|---|---|
| progressing normally; already in band; or in a harder-version round that gives useful signal | let it run |
| one authoring stage (world or task) over about 45 min (3–4× the ~12/~10-min target) | inspect its trajectory |
| ↳ it's stuck: no new agent steps for 15 min, the same commands or errors repeating, or waiting on a dead process | cancel it (`POST /v1/tasks/<id>/cancel` with a `note`) and log why |
| ↳ it's slow but making real progress | let it run |
| about 1.5 h without a first verdict | cancel, unless it's clearly still informative |
| the same infra error twice in a row | cancel, then fix the infrastructure in git |

## Hill-climbing rules

- **The metric:** the in-band rate (1–3/5), per family and in total.
- **Keep or revert:** keep the batch's changes if the in-band rate doesn't drop. If two
  consecutive batches get worse, revert, riskiest change first.
- **Confirm before trusting:** confirm a change that looks good on one wider batch of 10–12
  before building on it. One batch of 6 can be luck.
- **Keep changes small:** fine tweaks, never sudden or drastic rewrites.
- **Difficulty:**
  - mostly 0/5 (far too hard): start easier (`dial: 1`) and climb;
  - mostly 5/5 (too easy): raise the difficulty.
- **Diversity:** spread mechanisms within each family, and don't repeat a scenario.

## Limits

- **Budget:**
  - stay within the **$220 total cap** (`/healthz` shows the spend);
  - never raise any cap, global or per batch.
- **Timeouts:** never set a short solver timeout. Timeouts don't count as the model failing.
- **Changes go through git:** every change is committed, pushed and deployed. Live inspection of
  the cluster is fine; live editing of files in pods isn't (a deploy erases it, and nobody can tell
  later what ran).
- **Never:**
  - Terraform;
  - `make aws-k8s-down`;
  - deleting RDS or S3 data;
  - changing instance or security-group settings;
  - turning mock mode on or off;
  - scaling generation or executor above 10 (RDS micro's connection limit; DEPLOY.md §4.4).
- **If a deploy fails on AWS credentials** (`ExpiredToken`): stop deploying. Keep monitoring
  and tuning locally, note it in the log, and leave the batch running.
- **One owner for prompts tonight:** this agent only. No other session changes prompts.

## Monitoring the cluster

```bash
make aws-k8s-status                       # nodes, pods, services
make aws-k8s-logs SVC=generation          # or api|control|validate|executor
curl -s $SDE_URL/healthz                   # status, DB, budget
```

If a pod crash-loops or the API is down:
1. Read the logs.
2. Fix the cause in git.
3. Redeploy (after cancelling the running batch).
4. Restart the batch, and note it in the log.
