# Load test: finding the bottleneck, first with mock LLM calls, then in production

## First → now: how the system evolved (2026-11-17 → 2026-11-19)

Each row is one optimization (or a group of them), with the number measured before it and the
number measured after. The sources are this doc, docs/CAPACITY.md, docs/ITERATION_LOG.md and the
job records.

**How the job-record rows were computed** (validate, the queue waits, create and rollout times):
they are p50 / p90 over the job records of decided tasks (n = 15-130 per row), split by when the
job was created:
- **early**: before 12:00 UTC on 11-18;
- **day**: 12:00-20:00 UTC;
- **night**: from 20:00 UTC.

A job's `started_at` keeps its first start, so restarted jobs overstate their run time.

| Area | Optimization | First | Now | Change |
|---|---|---|---|---|
| **Cluster** | 8 Compose runners → 17 EC2 instances on K3s (9 on-demand + 8 Spot) | 16 vCPU | **34 vCPU** | 2.1x |
| Database | db.t4g.micro → db.t4g.medium; bounded pools + a heartbeat pool (`5f86caa`) | connections exhausted ("remaining connection slots are reserved"), then 66 of 79 held (84%) | **88 of 400 (22%)**, 0 pool waits, 0 errors | 5x headroom |
| Authoring capacity | generation on every runner (`4c7be83`), then 11 pods × 3 sessions | 2 sessions (control runner only); create jobs queued a median **21 min** | **33 sessions** | 16x |
| Authoring queue wait (create) | the capacity above, plus drains | day: p50 124 s, **p90 2,589 s** (43 min) | night: p50 0 s, **p90 12 s** | p90 216x lower |
| World queue wait | the same | day: **p90 3,646 s** (61 min) | night: **p90 52 s** | p90 70x lower |
| Validate | the author's stamped self-checks are adopted (`ab466d2`) | early: run **p50 127 s**; round 1 (mock): ~260 s unloaded, 363 s under a burst | night: run **p50 3 s, p90 5 s** | ~40x faster |
| Claim bursts | claim pacing 10 s (`41b25be`), CPU guard (`b31ff23`) | validate p50 363 s, rollout queue wait p50 177 s (mock, 30-task burst) | validate **263 s**, rollout wait **91 s** | 28% and 49% lower |
| Model stalls in authoring | call timeout 600 s / 1200 s for worlds (`09821ad`) | litellm default 6,000 s; one world spent **32.5 of 61 min** stalled (one call 1,214 s for 310 tokens) | a stalled call ends at 600 s (1,200 s for a world) | worst stall 10x shorter |
| Polling in authoring | 60 s waits, not 10 s polls (iteration 1 prompts) | 36% of agent steps empty; **46%** of prompt tokens on them | **28%** of steps, **29%** of prompt tokens | 37% fewer polling tokens |
| Abandoned jobs | suspect isolation capped at `SDE_SUSPECT_MAX_WAIT_S` (`f743fd1`); release at shutdown | an abandoned create waited **~80 min**, a rollout hours | another process takes it after **≤600 s**; a shutdown hands it back **at once** | ≥8x faster recovery |
| Deploys and scale-downs | drains (`de83f9c`, `688012f`), release refund (`84cf067`), Spot hand-back (`3b7a2b7`) | a deploy killed every running session: e.g. all 5 aws-w1-l3 worlds restarted from scratch at 20:55; scale-downs dead-lettered healthy runs | sessions finish (up to 60 min), agent runs up to 3,700 s; 2 scale-downs, **0** re-claims, **0** new dead letters | no lost work |
| Executor placement | spread + CPU requests the scheduler can fit (`3055a73`, `70878d6`, `5cd4b52`) | executors stacked at load 4-6 on 2 vCPU; then a 1,800m request: pods **Pending 11 min** ("0/17 nodes: Insufficient cpu") | 500m + 400m dind: pods schedule | unblocked |
| Agent runs per verdict | early stopping (`af4cdb8`) | always 5 runs | a certain verdict stops early: aws-w1-l3-t03 was accepted after **3** | up to 40% fewer runs on such versions |
| Agent-run reliability | classify, heartbeats that survive store errors, the terminal-close fix | INVALID from reclaims (`FileNotFoundError: 'ps'`), lapsed heartbeats, a closed terminal after a pass | **0 INVALID in 230** mock runs; reclaims adopt finished trials | |
| **Yield** | prompts, gates, the Trace Parser, world mutations (ITERATION_LOG) | iteration 0: **0 of 10** in band; iteration 1: 0 of 6 | **21 accepted** production tasks by 00:05 UTC 11-19 (5 by 20:50) | 0 → 21 |
| **Cost per accepted task** | the same | **~$40** of model spend per accepted task (20:50: $203 for 5) | **~$23** ($480 for 21) | 43% lower |
| Prompt size | caps over every spec (`6d56823`) | 3,500 / 2,200-token caps measured on one fixture each | real worst case **3,536 / 2,688** tokens, guarded at 3,650 / 2,800 | now measured |

What hasn't improved:
- **Agent-run queue wait went UP:** day p50 21 s → night p50 **149 s**, p90 575 s. Agent runs
  are CPU-bound (~1 vCPU each) and capped by executor pods. More tasks reach the agent-run
  stage now, so this is the next wall after authoring (§0.3 #11).
- **Run times are mostly the model's thinking:** create ~23 min p50, a world ~35 min, an
  agent run ~9 min. Infrastructure can't shorten those.

Status:
- **Round 2 (§0, 2026-11-18 evening → 2026-11-19 00:05 UTC):** measured on the production
  K3s cluster under real load (six lanes, real models).
- **Round 1 (§1-§7, 2026-11-18 early):** mock LLM calls on 8 EC2 runners, stopped by the user
  after run A. It's kept as history. Its §7 recommendations are mostly done; §0.4 says which.

Owner: infrastructure session.

## 0. Round 2: production on K3s, real models (2026-11-18 → 19)

No new mock run was done. These are measurements of the production cluster while six lanes
kept ~25-60 real tasks in flight, plus the optimizations shipped during the night (commits
below).

**In one paragraph.**
- **The cluster grew** from 8 EC2 runners on Docker Compose to **17 EC2 instances on K3s**
  (9 on-demand + 8 Spot, m7i-flex.large, 34 vCPU) with RDS **db.t4g.medium** (400
  connections). Round 1's two walls (CPU and RDS connections) were moved.
- **Theoretical:** agent runs are still the bottleneck (~1 vCPU each, CPU-bound), then
  authoring.
- **In practice:**
  - for most of the night it was **work supply**: authoring sessions sat idle between waves;
  - since the 23:55 "new worlds" push it is **authoring**: 33 sessions, all busy, 9 worlds
    queued;
  - throughout, **yield, not throughput**: most rejections are the leak review, not anything
    the cluster does.
- **The infrastructure fixes were about not losing work:**
  - drains on deploy, Spot hand-back, refunded attempts;
  - authoring call timeouts, heredoc self-heal;
  - pod CPU requests the scheduler can actually fit;
  - early stopping, which skips agent runs whose verdict is already certain.

### 0.1 The cluster now

| Resource | Round 1 | Now | Commit |
|---|---|---|---|
| Machines | 8 EC2 runners, Compose | **17 EC2 instances, K3s** (1 server + 16 agents), 9 on-demand + 8 Spot | `af545f8`, `d80d56f`, `e0c8857` |
| vCPU / RAM | 16 / 64 GB | **34 / ~129 GiB allocatable** | |
| RDS | db.t4g.micro, 79 connections | **db.t4g.medium, 400** | `71b6943` |
| Authoring (generation pods) | generation on every runner, 2-3 sessions | **11 pods × 3 sessions = 33**, CPU request 100m app + 100m dind | `c8d1cbe`, `2e422dc` |
| Agent runs (executor pods) | 8 Docker slots per runner | **KEDA 2-16 pods × 2 runs = up to 32**, request 500m / 1Gi (+400m dind) | `4ec4c2b`, `70878d6`, `5cd4b52` |
| Validate | per runner | **4 pods × 3 slots** | |
| Spot | refused (Free plan / SCP) | 8 Spot nodes; AWS Node Termination Handler; validate kept off Spot | `40aa1be`, `3b7a2b7` |

### 0.2 Live snapshot (2026-11-19 00:03 UTC, just after the "new worlds" push)

| Metric | Value |
|---|---|
| Running jobs | 58: **25 world**, 20 agent runs, 6 create, 4 revise, 2 analyze |
| Queued | **9 worlds READY**: authoring is full |
| Pods | generation 11 (+2 draining), executor 8 (+8 draining after the deploy), validate 4 |
| Node CPU (`kubectl top`) | 1 node at **99%**, 4 at 51-65%, 3 at 23-37%, **9 at ≤14%** |
| Node memory | 12-52% |
| RDS connections | **88 of 400**: 58 pool + 26 heartbeat idle, 2 active |
| Guards, last 2 h (8 sampled pods) | `guard: cpu` 2-15 per pod; **0** DB pool waits or DB errors |
| Spend on the books | **$479.93** (from $202.73 at 20:47: about $85/h on the books; the ledger prices prompts uncached, so the gateway's own figure is lower) |
| Accepted production tasks | 21 (their own cost $137; total spend / accepted ≈ $23) |

### 0.3 Bottlenecks found tonight, in the order they bit

| # | Bottleneck | Evidence | Fix | Commit |
|---|---|---|---|---|
| 1 | **Stalled model calls inside authoring** | iter1-t04's world: 61 min = 32.5 min of stalled calls (one 1214 s for 310 tokens); litellm's default timeout was 6000 s | call timeout 600 s for authoring, 1200 s for worlds at reasoning high | `09821ad` |
| 2 | **Deploys and scale-downs killed or re-ran work** | jobs "released at worker shutdown" re-ran from scratch; KEDA scale-down dead-lettered healthy agent runs | generation drains on deploy (sessions finish, up to 60 min); executor drains up to 3700 s; a release refunds the attempt | `de83f9c`, `688012f`, `84cf067` |
| 3 | **Spot interruptions** | the 2-minute notice is shorter than a drain | the termination handler hands jobs back at once (deploys still drain) | `3b7a2b7`, `40aa1be` |
| 4 | **Executors piled on a few nodes** | the scheduler can't see dind's CPU: executors stacked to load 4-6 on 2 vCPU while other nodes idled | spread constraints; CPU requests. An 1800m request fit no node (pods Pending 23:22-23:33, "Insufficient cpu"), so 500m app + 400m dind; generation's requests cut to 100m + 100m (measured) | `3055a73`, `4ec4c2b`, `70878d6`, `5cd4b52`, `2e422dc` |
| 5 | **Authoring terminals wedged** | an unclosed heredoc (aws-w1-l3-t01's world: `</EOF>`, 23 min without a step); a 35 KB heredoc (aws-w1-l5-t05 v2) | commands under ~4 KB; the terminal says so at every heredoc continuation line; the 30 worlds started on pre-fix pods were cancelled at 23:52 | `d1ad21c`, `6b42dca` |
| 6 | **Agent runs spent on decided versions** | a version with 1 pass and 2 fails after 3 runs is in band whatever the last 2 do | early stopping: decide once the verdict is certain, skip the rest (aws-w1-l3-t03 was accepted after 3 runs) | `af4cdb8` |
| 7 | **Timeouts treated inconsistently** | an agent run at the 1-hour limit | it waits for the Trace Parser's ruling; a timeout isn't a failure by itself | `856db8c`, `f1c822e` |
| 8 | **Validate re-ran the author's checks** | 4 trials (~4 min) per bundle, already run by `sde-task` | validate adopts stamped author checks when exact: validate takes **~4 s** | `ab466d2` (`SDE_ADOPT_AUTHOR_CHECKS=1`) |
| 9 | **Authoring self-check memory** | `sde-task run` roles in parallel exceeded the 5-6 GiB dind | roles run one at a time per session (`SDE_TASK_PARALLEL=1`), 3 sessions a pod | `082166f`, `77002d7` |
| 10 | **Work supply** (most of the night) | idle authoring sessions between waves (10 of 42 busy at one point) | six lanes keep ~5-6 tasks each in flight and top up per decision; at 23:55, new-world batches across lanes | the method (HILLCLIMBING.md) |
| 11 | **Authoring capacity** (now) | 33 of 33 sessions busy, 9 worlds READY at 00:03 | the next lever is more generation pods. They're cheap in CPU (~0.4 GB, model-bound) but each self-check spikes to 1 CPU / 2 GB | not done |

Not bottlenecks tonight:
- **RDS:** 88 of 400 connections, 0 waits or errors;
- **Redis and S3:** no errors;
- **memory:** ≤52% on every node.

The CPU guard fires a few times an hour per pod, so it still protects against bursts.

### 0.4 Round 1's follow-ups (§7), where they stand

| Round 1 follow-up | Status |
|---|---|
| 1. RDS connections before scaling out | **Done**: db.t4g.medium, 400 connections; 88 used with 17 nodes |
| 2. More CPU for more throughput | **Done**: 34 vCPU (17 m7i-flex.large); a larger on-demand quota request (48 vCPU) is open |
| 3. Verifier toolchain reinstalled per trial (~15 s) | Not done (the user's call; it changes every generated task) |
| 4. Finish runs A-C and a 60-task confirm (mock) | Not done; production load replaced it |
| 5. A real-wait profile | Measured live instead: model-bound authoring, CPU-bound agent runs (§0.2) |
| 6. Mock robustness | Not done; `SDE_MOCK_LLM=0` in aws-prod |

### 0.5 Throughput now

| Stage | Capacity | Per task | Theoretical tasks/h |
|---|---|---|---|
| Authoring | 33 sessions | a new world ~30 min + create ~10-15 min (+ repairs) | **~40** |
| Agent runs | up to 32 (16 pods × 2) | 5 × ~15 min; early stopping often skips 1-2 | **~25-30** |
| Validate | 12 slots | ~4 s adopted | hundreds |

Real tasks/h is set by the in-band rate. Each accepted task took 1-4 versions, and every
rejection decided between 22:35 and 23:15 UTC was a blind leak failure on three drafts. So the throughput lever that
matters most is yield (TASK_GENERATION_RULES.md, the leak review), not cluster size.

### 0.6 How to re-measure

- **Node CPU and pods:** run `kubectl top nodes` and `kubectl get pods` through
  `deploy/k8s_aws.py`'s `remote` on runner 0.
- **Queue:** `GET /v1/queue` (READY = waiting, LEASED = running).
- **Spend:** `GET /healthz`.
- **RDS connections:** `pg_stat_activity` from `deploy/control`.
- **Guard and pool signals:** `kubectl logs … | grep -cE 'guard: cpu|waited .* for a database connection'`.
- **Limits:** `deploy/k8s/overlays/aws-prod/kustomization.yaml`; see also docs/CAPACITY.md.

---

# Round 1: mock LLM calls on 8 EC2 runners (2026-11-18, history)

**In one paragraph.** With every model call mocked ($0 spend), the 8-runner AWS cluster ran
10 and 30 fixture tasks end to end: 230 rollouts, **0 INVALID**. **CPU is the bottleneck**:
runners hit a load average of 6-8 on 2 vCPU (300-400%) while memory stayed 72-84% free. The
database, Redis and S3 never made a job wait (0 pool waits, 0 DB errors), but RDS sits at
**66 of 79 connections**, so connections are the next wall after CPU. Docker image builds are
*not* the cost (cached per runner); per rollout, the verifier is (it reinstalls its toolchain:
~15 s, then the tests: ~30 s). The uneven load (2 of 8 runners idle at the start) came from a
claim burst that the lagging load average couldn't stop. **Claim pacing** fixed it: under the
same 30-task burst, validate time went back to its unloaded baseline (p50 363 s → 263 s) and
rollout queue wait halved (p50 177 s → 91 s).

The goal was to push the AWS cluster with model calls replaced by short sleeps:
- 8 runners, 2 vCPU / 8 GB each;
- RDS db.t4g.micro, ElastiCache Redis, S3.

Push until something other than model wait limits throughput, fix it, and repeat.

The method follows the earlier load test on the expert-capture platform
(`~/IdeaProjects/expert-capture-platform/docs/LOAD_TEST.md`). Jobs that mostly *wait* on a
model are bounded by how many are allowed in flight, until some other resource saturates.

```
throughput per runner ≈ slots / (model wait + CPU time per job)    while nothing else saturates
cluster throughput    ≤ vCPUs / CPU-seconds per job                 once CPU is the limit
                      ≤ DB connections / (connection-seconds per job) once the DB pool is
```

## 1. What we expected (the hypothesis)

Real P1 numbers (local, one task): the world took 5.4 min / $0.51, and the task 21.6 min / $2.39.
Most of that is glm-5.1 thinking (1-3 min per agent step): wait, not CPU.

| Work | Where | Expected cost | Measured (§6) |
|---|---|---|---|
| World and task image builds | world_check, sde-task, validate, every rollout's env build | CPU-heavy, minutes | **2-3 s per trial**: the image is cached per runner after the first build |
| Test runs | validate (oracle ×2, nop, wrong fix), rollouts' verifier | seconds to minutes | **~45 s per trial**: ~15 s toolchain install + ~30 s of the fixture's tests |
| Agent commands in rollout containers | terminus-2's shell commands | bursts of CPU | not exercised (oracle / nop) |
| Container start/stop, image pulls | everywhere | disk + Docker daemon | inside Harbor's ~15 s overhead per trial |
| Artifact upload/download | S3 | network, threads | ~25 s per rollout outside the trial, with Harbor CLI start; no errors |
| DB: claims, heartbeats, settles | every job | RDS connections (micro ~80) | 0 waits, but **66 / 79 connections held** |

Open questions going in:
- **(a)** How many CPU-seconds per job type does the non-model work take?
- **(b)** At what concurrency does CPU, memory, Docker, RDS or Redis saturate?
- **(c)** Do the slot ceilings cap throughput below what the machines can do?

Answers:
- **(a)** See the breakdown in §6.3.
- **(b)** CPU first, at the first 30-task burst. Memory, Redis and S3 were never close; RDS
  connections are next.
- **(c)** No, the opposite: 8 Docker slots on 2 vCPU are more than the CPU can serve. The
  CPU guard, not the ceiling, was what bound.

## 2. Mock mode, as built

The user decided the scope: **one feature flag on the real code, plus a small driver script.**
No new mock subsystem. The fuller design from the plan (mock authoring and rollout agents) was
dropped: Harbor's own `oracle` / `nop` agents and the existing task injector cover what this
round needed.

| Piece | File | What it does |
|---|---|---|
| Model calls | `shared/llm/mock.py`, hook at the top of `shared/llm/llm.py` `complete()` | With `SDE_MOCK_LLM=1`, every model call sleeps `SDE_MOCK_LLM_S` (±20%), then returns a schema-valid answer at cost 0, never cached. Registered answers cover the formats where the schema's first value would mean "fail": `leak_review` → clean; `blind_judgement` → `none`. Everything else is built from the strict schema (first enum value, required fields, empty arrays). Covers the leak review (three calls), one-shot drafts, and the Trace Parser's reviews. |
| Rollouts | `evaluation/harbor.py` | With `SDE_MOCK_LLM=1`, `pinned_agent()` returns `mock:oracle\|nop`. Each rollout holds its slot for `SDE_MOCK_ROLLOUT_S` (±30%, cancellable), then runs Harbor's built-in `oracle` agent with probability `SDE_MOCK_PASS_P`, else `nop`. No model, no agent config pinned (`classify` checks none). Real image build, real verifier, real reward. |
| Settings on AWS | `deploy/remote-deploy.sh` | `SDE_MOCK_LLM`, `SDE_MOCK_LLM_S`, `SDE_MOCK_ROLLOUT_S`, `SDE_MOCK_PASS_P` come from SSM (`/sde/mock_llm`, `/sde/mock_llm_s`, `/sde/mock_rollout_s`, `/sde/mock_pass_p`); off unless `/sde/mock_llm` is `1`. |
| Tasks | `scripts/inject_task.py` (existing) | Puts a task directory into the artifact store and creates the task row with its `validate` job, exactly as the Generation Service would. From there everything is real: gates, 5 rollouts, Trace Parser, decision. |
| Fixture | `tests/fixtures/p1-local-1/task` | P1's real task: a `reservation` service with a lease/crash/redelivery bug and 8 pytest tests. Shipped in every release (from `2f9d456`). |
| Driver | `scripts/loadtest.py <step> <n> [--interval S] [--no-inject] [--timeout S]` | Injects n copies (`lt-<step>-NNN`), all at once or one every `--interval` s. Samples the queue every 15 s and waits until every task is decided. Prints JSON: wall time, tasks/min, peak READY / LEASED per type, and per type the count, INVALID count, p50/p95 queue wait and run time. `--no-inject` reports on a step that already exists. |

Settings used in every run: `SDE_MOCK_LLM_S=3`, `SDE_MOCK_ROLLOUT_S=30`, `SDE_MOCK_PASS_P=0.4`.

**Not exercised in this round:**
- authoring (world / create / revise agents): the local real batch measures that;
- terminus-2's own CPU in the rollout container (the oracle runs `solve.sh`, nop runs nothing);
- the real model's wait, which in real runs dominates (minutes per step).

So these numbers describe the pipeline's own cost at high turnover. Real runs hold each
slot ~15 min for the model, so they reach the CPU limit at a much higher task count.

How to run a step (from a laptop with AWS access; the scratch helpers wrap `aws_ops.remote`):

```
# on the control runner (aws_ops.runners()[0]): a detached one-off container with the
# services' environment and the fixture mounted from the release dir
cd /opt/sde/current && docker compose --env-file /opt/sde/runtime.env -f deploy/compose.prod.yaml \
  run -d --name lt-s30 -v /opt/sde/current/tests/fixtures:/fixture:ro --entrypoint python worker-control \
  scripts/loadtest.py s30 30 --task-dir /fixture/p1-local-1/task
docker logs lt-s30          # the JSON report, when it's done
```

The driver must run detached: an SSM command that times out kills its process tree, and the
first attempt at step 1 died that way after injecting.

## 3. What was measured, and how

| Measurement | Source |
|---|---|
| Queue wait, run time, INVALID, outcome per job | `jobs` table (`created_at`, `started_at`, `finished_at`, `outcome`) via `scripts/loadtest.py` |
| Queue depth and running jobs over time | the driver's 15 s samples (READY / LEASED by type) |
| Per-runner load average, free memory, containers | `/proc/loadavg`, `free`, `docker ps` over SSM, about every minute |
| Harbor phase timings | each trial's `result.json` in S3 (`traces/<job>/.../result.json`): `environment_setup`, `agent_setup`, `agent_execution`, `verifier` |
| Test durations | the verifier's `ctrf.json` |
| Guard skips, DB pool waits, DB errors | container logs: `guard: cpu`, `guard: memory`, `waited … for a database connection`, `connection slots`, `OperationalError` |
| RDS connections | `pg_stat_activity` by `application_name` (`sde-pipeline`, `sde-pipeline-heartbeat`), during run A |

Not measured (the plan's §3 asked for them):
- per-runner CPU % series written to S3, and `stages_ms` per job;
- RDS CPU from CloudWatch.

Load average plus Harbor's own phase timings were enough to locate the cost.

## 4. Procedure, as run

| # | Run | Tasks | Arrival | Docker slots / runner | Claim pacing | Status |
|---|---|---|---|---|---|---|
| 1 | step 1 (`lt-s10`) | 10 | burst | 8 | off | **done** |
| 2 | step 2 (`lt-s30`) | 30 | burst | 8 | off | **done** |
| – | run A, attempt 1 (`lt-a30`) | 30 | burst | 8 | 10 s | **invalid**: mock bug (§6.6) |
| – | run A, attempt 2 (`lt-a30b`) | 30 | burst | 8 | 10 s | **invalid**: cached bad review (§6.6) |
| 3 | run A (`lt-a30c`) | 30 | burst | 8 | 10 s | **partial**: snapshot taken mid-run, then the test was stopped |
| – | run B | 30 | burst | 4 | 10 s | not run |
| – | run C | 30 | 1 per 20 s | 8 | 10 s | not run |
| – | confirm | 60 | best of A-C | best | best | not run |

Instead of 60 → 120 copies of the same burst, runs A-C varied one thing at a time:
- pacing (A);
- the slot ceiling (B);
- the arrival pattern (C).

A bigger identical burst would only have re-shown the CPU overload. The user stopped the test
after run A's snapshot. B, C and the confirm run are follow-ups (§7).

Fixed for every run:
- 8 runners;
- `SDE_AGENT_SLOTS=3`, `SDE_GENERATION_CONCURRENCY=8`, `SDE_DB_POOL_MAX=2`;
- memory guard at 20% free, CPU guard at 90% of the 1-minute load average.

Settings verified in each runner's worker container with
`docker compose exec worker-runner env | grep SDE_`.

## 5. Likely fixes: what the data said

| Plan's candidate fix | Verdict |
|---|---|
| Raise the slot ceilings; let the memory guard be the limit | **Wrong for this hardware.** Memory never got close (72-84% free). CPU binds long before 8 Docker jobs per 2 vCPU. A **CPU guard** was added as the backstop. |
| Prebuilt per-environment base images (sde-base-*) | Not for build time (builds are cached). Possibly for the **verifier toolchain**, which is reinstalled every trial (§7). |
| Build each task image once per bundle | **Already effectively true**: `environment_setup` is 2-3 s per trial after a runner's first build. |
| Split slot pools by weight | Not needed yet. |
| RDS: small pools, reserved heartbeat connection, fewer queries | Done (pool max 2 plus a 1-connection heartbeat pool). **0 waits**, but 66/79 connections held, mostly idle. Next to act on (§7). |
| Limit concurrent `docker build`s | Not needed: builds were cached. |
| Gateway rate limits | Real runs only; not tested here. |

## 6. Results

### 6.1 Per-job baseline (step 1: 10 tasks, no queueing)

| Job | n | Queue wait p50 / p95 | Run p50 / p95 | INVALID |
|---|---|---|---|---|
| validate | 10 | 0.6 / 0.9 s | **260 / 292 s** | 0 |
| rollout | 50 | 0.6 / 1.5 s | **118 / 163 s** | 0 |
| analyze | 50 | 0.9 / 8.7 s | ~0 / 11 s | – |

- Outcomes: 26 VALID_PASS / 24 VALID_FAIL at `SDE_MOCK_PASS_P=0.4`. 9 accepted (in band).
- 1 out of band went to `revise`, which FAILED as expected: injected tasks have no draft record
  (`work/drafts/<sha>.json`) to revise.
- 3 analyze jobs went DEAD with `cancelled: task ACCEPTED`. That's expected: the task was
  decided before they ran.

### 6.2 Step 2: 30 tasks in one burst, no pacing

| | Step 1 (10 tasks) | **Step 2 (30 tasks)** |
|---|---|---|
| Wall time (inject → last decision) | – | **990 s (16.5 min)** |
| Throughput | – | **1.82 tasks/min** |
| Validate run p50 / p95 | 260 / 292 s | **363 / 466 s** (+40%: CPU contention) |
| Rollout run p50 / p95 | 118 / 163 s | **150 / 174 s** (+27%) |
| Rollout queue wait p50 / p95 | 0.6 / 1.5 s | **177 / 331 s** |
| Validate queue wait p50 / p95 | 0.6 / 0.9 s | 0.2 / 0.7 s (all 30 claimed at once) |
| Peak running | – | 30 validates, then **51 rollouts of 64 Docker slots** |
| Peak rollouts READY (waiting) | – | 92 |
| INVALID rollouts | 0 / 50 | **0 / 150** |
| Decided | 9 accepted, 1 rejected | 26 accepted, 4 rejected (out of band → revise → no draft) |

Runner load during step 2 (1-minute load average on 2 vCPU; 100% = 2.0):

| Time | Six busy runners | Two others | Free memory |
|---|---|---|---|
| +1.5 min | 3.8-7.2 | 0.2, 1.0 | 73-82% |
| +5 min | 4.0-8.3 | 0.1, 1.0 | 72-82% |
| +8 min | 5.8-8.2 | 0.1, 0.2 | 72-84% |

**Uneven spread:**
1. All 30 validates were claimed within seconds, by six runners: the first to poll took up
   to 8 each.
2. The 1-minute load average lags a burst by about a minute, so the CPU guard could not stop
   the grab.
3. Two runners did one validate each, found the queue empty, and sat near idle while the
   others ran at 300-400%.
4. Later, once rollouts became READY, the busy runners' CPU guard was blocking claims, and
   the idle ones took work.

It wasn't Redis candidate order or generation pinning: every runner had the same settings
and the same queue.

### 6.3 Where the time goes (Harbor `result.json` + `ctrf.json`, step 1)

One rollout (`lt-s10-002-v1-rollout1-r0`, 118 s end to end):

| Part | Time | Notes |
|---|---|---|
| Mock model hold (`SDE_MOCK_ROLLOUT_S`) | ~30 s | stands in for terminus-2 + the model |
| Harbor `environment_setup` | 3.0 s | the env image was cached: **builds are not the cost** |
| `agent_setup` + `agent_execution` | 1.4 s | oracle runs `solve.sh` |
| `verifier` | **47.7 s** | see below |
| Rest of the Harbor trial (container start/stop, copies) | ~14 s | trial total 66.4 s |
| Outside the trial | ~22 s | `harbor run` CLI start-up, bundle fetch from S3, trace upload, settle |

Inside the verifier (`tests/test.sh`, the standard Terminal-Bench pattern):

| Step | Time | Notes |
|---|---|---|
| `apt-get install curl` (if missing), download `uv`, `uvx` fetches Python 3.13 + pytest | **~15 s** | every trial, from the internet; CPU for unpacking |
| The fixture's 8 tests | **~30 s** | `test_fixed_seed_failure_schedules_preserve_accounting` alone takes **24.4 s** (schedule exploration, CPU-bound); the other 7 take 0.5-1.2 s each |

One validate gate trial (`oracle1`, 56.6 s): `environment_setup` 2.3 s, agent 1.0 s,
verifier 41.6 s. A validate runs 4 trials (oracle ×2, nop, wrong-fix), plus the wrong-fix
bundle's own image build and three mocked leak-review calls (~3 s each). That makes
**~260 s unloaded**.

So per task, the non-model CPU is roughly:
- validate: 4 × ~45 s of verifier;
- rollouts: 5 × ~45 s of verifier;
- plus Harbor and Docker overhead.

That's about 400+ CPU-seconds, about 7 CPU-minutes per task. The cluster has 16 vCPU.

```
cluster ceiling ≈ 16 vCPU × 60 s / ~400-500 CPU-s per task ≈ 2-2.4 tasks/min
measured (step 2): 1.82 tasks/min, with the burst's poor spread
```

This fixture's 24 s CPU-heavy test makes it more expensive than an average task. Real tasks
vary.

### 6.4 Run A: 30 tasks in one burst, claim pacing 10 s (partial)

Taken mid-run, when the user stopped the test: all 30 validates done, 1 task accepted, 1
rejected, 28 still evaluating. The run-time percentiles are over finished jobs only; the queue
waits are over started ones.

| | Step 2 (no pacing) | **Run A (pacing 10 s)** |
|---|---|---|
| Validate run p50 / p95 | 363 / 466 s | **263 / 323 s**: back to the unloaded baseline (260 / 292) |
| Rollout run p50 / p95 | 150 / 174 s | **140 / 160 s** |
| Rollout queue wait p50 / p95 | 177 / 331 s | **91 / 137 s** (started rollouts so far) |
| Peak rollouts running | 51 | 49 |
| Peak rollouts READY | 92 | 89 |
| INVALID rollouts | 0 / 150 | **0** so far |
| Wall time / throughput | 990 s / 1.82 per min | not measured (stopped) |

Mid-run, pacing and the CPU guard together held running rollouts to 16 (118 READY) for a
while, so pacing may slow the ramp-up after a burst.

Reading:
- Pacing spread the validate burst, so no runner ran 8 builds on 2 vCPU. Validates ran
  27% faster at p50, and the rollouts behind them started sooner.
- Whether it also raises **throughput** (tasks/min) is not measured: the run was stopped
  before the last decision.

### 6.5 Guards, database, queue

Container logs since the last deploy (run A's three attempts), per runner:

| Signal | Count |
|---|---|
| `guard: cpu` (logged at most once a minute per process) | **24-31 on every runner**: the CPU guard bound throughout |
| `guard: memory` | **0** |
| DB pool waits ≥ 2 s (`waited … for a database connection`) | **0** |
| DB errors (`connection slots`, `OperationalError`) | **0** |

RDS connections mid-run (`pg_stat_activity`, run A):

| application_name | State | Connections |
|---|---|---|
| `sde-pipeline` | idle | 55 |
| `sde-pipeline` | active | 1 |
| `sde-pipeline-heartbeat` | idle | 9 |
| **Total** | | **66 of `max_connections` = 79** |

The pool bound (`SDE_DB_POOL_MAX=2` per process, plus 1 heartbeat connection, since `5f86caa`)
works: no waits, no errors. But the pools keep their connections open while idle, so 8
runners × ~3-4 processes nearly fill the micro. A 9th runner, or more processes per runner,
would run into the cap. Before `5f86caa`, one connection per thread hit it outright
(`remaining connection slots are reserved for rds_reserved`); see "Fixes" below.

Redis and S3: no errors, no measurable waits. Analyze jobs (mocked, 3 s) waited at most
8.7-17 s at p95.

### 6.6 Incidents during the test (runs lost, and why)

| # | What happened | Cause | Fix | Cost |
|---|---|---|---|---|
| 1 | Step 1's driver died right after injecting; no report | The driver ran under `nohup` inside an SSM command. SSM's timeout killed the whole process tree | Run the driver as a detached container (`compose run -d`) and read `docker logs`. Step 1's report was recomputed with `--no-inject` | none (the jobs ran anyway) |
| 2 | Run A, attempt 1: **all 30 validates FAILED** (leak gate) | `main` merged in `-75`'s blind leak review (`152b0b6`): a reviewer sees only the instruction, and a judge answers `specific` / `generic` / `none`, where `specific` is a leak. The mock built answers from the schema, taking the **first enum value**: `specific` | `b1f0f67`: register `blind_judgement → none`; `tests/shared/llm/test_mock.py` now fails if any mock answer reads as a leak | ~17 min of cluster time, $0 |
| 3 | Run A, attempt 2: **all 30 FAILED again**, with the fix deployed | The leak review is stored per bundle (`<work>/leak_reviews/<bundle sha>/review.json`) and adopted by later jobs. All injected tasks share the fixture's bundle, so attempt 1's bad verdict was reused on every runner | Deleted that one mock-made record on all 8 runners (`/opt/sde/work/leak_reviews/e49d3999…`) | ~10 min, $0 |
| 4 | A local `make test-infra` failed during the work | The local test Redis's AOF file was corrupt | `redis-check-aof --fix` on the local `deploy_redisdata` volume (only this worktree's test data; `-c0`'s `sde-live` stack has its own volume) | none |

Lessons:
- A schema-built mock answer is fragile whenever a check adds a question: the test now guards
  the leak gate's formats.
- Cached per-bundle results plus identical fixture bundles mean a bad result poisons every
  later copy. When the mock changes, clear `leak_reviews/<fixture sha>` on the runners. Or
  make the fixture unique per run (a follow-up; §7).

### 6.7 Fixes made during this work, with commits

All are on `infra-authoring`; `-c0` merged them into `main` (through `e584a29`, and later).

| Commit | Fix | Effect |
|---|---|---|
| `4c7be83` | Generation runs on **every** runner (`SDE_RUNNER_GENERATION_CMD`), 8 threads each (`SDE_GENERATION_CONCURRENCY`) | Before: generation only on the control runner, 2 at a time; a batch's create jobs queued for a median 21 min |
| `5f86caa` | One bounded Postgres pool per process (`SDE_DB_POOL_MAX`, 2 on AWS) plus a dedicated 1-connection **heartbeat pool**; pool waits ≥ `SDE_DB_POOL_WARN_S` are logged | Before: one connection per thread exhausted the micro's ~80 (`remaining connection slots are reserved`); heartbeats failed, leases expired, running jobs were treated as abandoned. Now: 0 waits, 0 errors |
| `f743fd1` | **Suspect starvation (finding C):** after `SDE_SUSPECT_MAX_WAIT_S` (600 s) past its lease expiry, any process may claim an abandoned job (still isolated). The Redis queue keeps a suspect's id after a missed claim | Before: only a process with *no* live lease could take a suspect, and an executor always holds one, so abandoned jobs waited hours (aws-prompt-1: ~80 min on create, hours on rollout5) |
| `25d4970`, `2e0612c` | Mock mode (§2) and the driver | This test |
| `b31ff23` | **CPU guard**: no new Docker claims at ≥ `SDE_MAX_CPU_LOAD_PCT` (90) of the 1-minute load average (`/proc/loadavg`, the host's even in a container; none off Linux). Both guards log `guard: cpu\|memory`. Also: **compose.dev reads `deploy/local.env`** in every app service | The CPU backstop bound throughout this test. Locally, only 3 settings used to reach the containers, so the stack ran with **no budget cap** and default concurrency |
| `41b25be` | **Claim pacing**: a process waits `SDE_DOCKER_CLAIM_SPACING_S` between Docker claims (code default 0 = off; AWS 10 via `/sde/docker_claim_spacing_s`) | Run A: validate p50 363 → 263 s, rollout wait p50 177 → 91 s |
| `12d890d` | compose.dev: no `environment:` default may shadow `local.env` (compose lets `environment` beat `env_file`, even an empty `${VAR:-}`); `make up-agent` writes `deploy/make.env`; test | `local.env` alone now sets slots and author mode |
| `53b8a48`, `1f27d30` | Driver: `--no-inject`, `--interval` | Recomputed reports; trickle arrival for run C |
| `b1f0f67` | Mock: blind judge answers `none`; test | Incident 2 |

## 7. Recommendations and follow-ups

**Per-runner settings for the current hardware (2 vCPU / 8 GB):**

| Setting | Now | Recommended | Why |
|---|---|---|---|
| `SDE_DOCKER_SLOTS` (TF `docker_slots`) | 8 (load test) | **5** | 8 is never used on 2 vCPU: the CPU guard binds first, and the extra jobs only add contention. 5 is the default in `variables.tf`; run B (4 vs 8) would confirm it |
| `SDE_AGENT_SLOTS` (TF `agent_slots`) | 3 (load test) | **2** | Authoring is not exercised here. Keep the conservative default until the local real batch shows memory per authoring session |
| `SDE_GENERATION_CONCURRENCY` | 8 | 8 | No Docker slot (one-shot); no contention seen |
| `SDE_DOCKER_CLAIM_SPACING_S` | 10 | **10**, or ~5 if real runs show a slow ramp-up | It fixed the burst. Its throughput effect is not measured |
| `SDE_MAX_CPU_LOAD_PCT` | 90 | 90 | 0 INVALID at this level. No evidence that a lower threshold is needed |
| `SDE_MIN_FREE_MEMORY_PCT` | 20 | 20 | Never triggered |
| `SDE_DB_POOL_MAX` | 2 | 2 | 0 waits. Don't raise it on db.t4g.micro: see item 1 below |
| `SDE_MOCK_LLM` (`/sde/mock_llm`) | **1** | **set back to 0 before any real batch**, and redeploy | Otherwise the runners mock every model call and run oracle/nop rollouts |

Returning to real runs: `aws ssm put-parameter --name /sde/mock_llm --value 0 --overwrite`,
then `terraform apply` with the default `docker_slots=5` / `agent_slots=2` (no `-var`
overrides), then `aws_ops.py deploy` and `smoke`.

**Follow-ups, in priority order:**
1. **RDS connections before scaling out.** 66 of 79 are held, mostly idle. The budget:

   ```
   per process   ≤ SDE_DB_POOL_MAX (2) main + 1 heartbeat                   = 3
   per runner    3 processes (validate, executor, generation)             ≤ 9
   control adds  3 processes (api, reconciler, Trace Parser)              ≤ 9
   worst case    9 × runners + 9            ≤ 79 − ~3 reserved (rds_reserved) = 76
                 → at most 7 runners if every pool is full at once
   measured      66 with 8 runners (heartbeat pools open only while a job runs)
                 → ~7.5 per runner + ~6 for control: 9 runners ≈ 74, at the edge
   ```

   **So the micro instance supports 8 runners, and no more**, with `SDE_DB_POOL_MAX=2`. A 9th
   runner, `SDE_DB_POOL_MAX=3`, or another process per runner needs one of these first:
   - **(a)** a larger instance: db.t4g.small (~180 connections) at ~$0.03/h. It needs the AWS
     account upgraded from the Free plan (`db.t4g.small` was refused with
     `FreeTierRestrictionError`). The upgrade also allows instance types larger than 2 vCPU;
   - **(b)** pools that release idle connections (psycopg_pool `min_size=0`, `max_idle`), so
     idle processes hold none.
2. **More CPU, if more throughput is wanted.** The cluster ceiling is ~2-2.4 mock tasks/min
   with 16 vCPU. That needs either the account upgrade (4-8 vCPU instances, and a higher
   quota: the 18-vCPU request is pending) or more runners (blocked by the quota and by RDS
   connections, item 1).
3. **Verifier toolchain (user: later).** Every trial reinstalls curl, uv, Python 3.13 and
   pytest (~15 s of CPU, plus an internet dependency). The manager's suggestion: install
   pytest into a root-only venv at image build time in `shared/taskformat/materialize.py`, and have
   `test.sh` call it. That saves ~15 s per rollout and ~60 s per validate. It changes every
   generated task, so it's the user's decision.
4. **Finish the comparison:**
   - run A to completion (throughput with pacing);
   - run B (4 Docker slots);
   - run C (`--interval 20`, trickle arrival);
   - one 60-task confirm at the winner.
   Clear `/opt/sde/work/leak_reviews/<fixture sha>` on every runner first, or give each run a
   unique fixture.
5. **A real-wait profile.** `SDE_MOCK_ROLLOUT_S=600` (about a real rollout) would show the
   steady state real runs see: slots held long, CPU mostly idle, so the slot ceilings and the
   model's rate limits matter more than CPU.
6. **Mock robustness.** A mock answer for every strict format in the codebase (not only the
   leak gate), and a `result.mock` flag on jobs, so mock batches are labeled in reports. The
   plan asked for this; it isn't built.

**Real runs differ.** In a real rollout the model holds the slot ~15 min while the CPU idles,
so the CPU-heavy parts measured here are spread out. With real tasks, the model's speed and
the gateway's rate limits will usually bind first. CPU matters for throughput at scale, and
the guard plus pacing protect against bursts.

### 7.1 State of the cluster after the test

- Release `b1f0f67` on all 8 runners, with mock mode **on** (`/sde/mock_llm=1`).
- Slots 8 Docker / 3 agent (TF `-var` overrides), pacing 10 s.
- Run A's remaining 28 tasks (`lt-a30c-*`) keep draining at $0 until they're decided.
- All load-test tasks are prefixed `lt-` (`lt-s10-*`, `lt-s30-*`, `lt-a30-*`, `lt-a30b-*`,
  `lt-a30c-*`).
