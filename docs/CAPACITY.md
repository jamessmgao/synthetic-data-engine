# Capacity, throughput and cost

The production cluster as deployed on 2026-11-18 ~21:00 UTC (release 5e1073985063), with
utilization and cost measured the same evening. Re-measure with the commands at the end.

## The cluster

| Resource | Size | Notes |
|---|---|---|
| EC2 instances | **17**: 9 on-demand + 8 Spot, m7i-flex.large (2 vCPU, 8 GB) | 34 vCPU, ~129 GiB allocatable. On-demand quota 18 vCPU (a 48 vCPU request is open); Spot quota 32 vCPU (16 used); Spot only in the first subnet's AZ (no capacity in the second) |
| K3s | 1 server (runner 0: api, control, both tunnels, dashboard) + 16 agents | Spot nodes labelled `sde/lifecycle=spot`; validate stays off them |
| RDS | db.t4g.medium, **max_connections 400** | was db.t4g.micro (79) |
| Redis | ElastiCache | queue ids only; Postgres is the source of truth |
| S3 | sde-pipeline-artifacts-284460774261 | worlds, bundles, traces, leak reviews |

## Concurrency limits

| Stage | Pods | Per pod | **Concurrent** | Where it's set |
|---|---|---|---|---|
| Authoring (world, create, repair, mutation) | generation **11** | 3 sessions (SDE_AGENT_SLOTS), 6 docker slots | **33 sessions** | aws-prod kustomization `replicas`, generation.yaml |
| Agent runs (the solver's 5 per task) | executor **2-10** (KEDA, on queued runs) | 3 runs (SDE_DOCKER_SLOTS) | **6-30 runs** | aws-prod kustomization ScaledObject patch |
| Validate + world_check | validate **4** | 4 slots | 16 | aws-prod kustomization `replicas` |
| Analysis (Trace Parser) | control + workers | model calls | not a limit | |
| Database | all pods | pool 2 + heartbeat threads | ~70-140 in use of 400 | SDE_DB_POOL_MAX |

What each unit costs the machines:
- an authoring session: ~0.4 GB, almost no CPU (it waits on the model); its self-check
  containers spike to 1 CPU / 2 GB each, one at a time per session (SDE_TASK_PARALLEL=1);
- an agent run: **1 vCPU and ~2 GB for its whole duration** (the task container's limits).
So authoring is cheap and agent runs are CPU-bound: 30 runs ≈ 30 vCPU is the ceiling the 34 vCPU
leave after authoring (~3-4 vCPU) and the system (~3 vCPU). More executor pods would
oversubscribe the CPU and slow every run (and timing tests).

## Time per stage (measured, tonight)

| Stage | Typical | Range |
|---|---|---|
| World (agent builds the system) | ~30 min | 16-51 min (mutations 10-37 min) |
| world_check | seconds | 1-2 s when adopted |
| Create (agent writes the task) + leak review | ~10-15 min | 6-20 min; each repair ~5-6 min (max 2) |
| Validate | seconds | ~4 s when the author's checks are adopted |
| One agent run | ~10-20 min | 5-25 min (timeouts are retried, never failures) |
| Analysis | ~1-3 min | |
| A task's first verdict (v1) | ~1.5-2 h | from submission |
| Each revision (mutation → verdict) | ~1-1.5 h | |

## Throughput

| Stage | Theoretical tasks per hour | How |
|---|---|---|
| Authoring | **~40** | 33 sessions / ~50 session-min per task (world + create + some repairs) |
| Agent runs | **~24** | 30 runs / (5 runs × ~15 min) per task |
| Validate | hundreds | |

- **The theoretical bottleneck is agent runs (~24 tasks/h), then authoring (~40).**
- **The observed bottleneck is work supply:** the queue often has idle authoring sessions
  (10 of 42 busy at one point tonight) because tasks arrive in waves. The six lanes keep ~25-30
  tasks in flight and top up one per decided task, which is what fills the cluster.
- Revisions multiply the work per accepted task: tonight's accepted tasks took 1-3 versions.

## Utilization (2026-11-18, ~20:50 UTC, 25 new lane tasks in flight)

| Metric | Value |
|---|---|
| Node CPU | mostly 1-8%; busiest 81%, 33%, 20-23% (agent runs) |
| Node memory | 14-49% |
| Authoring sessions busy | 26 of 42 (before the resize to 33), 6 worlds queued during the rollout |
| Agent runs | 20 running, 0 queued |
| RDS connections | ~70 of 400 |

## Cost

| Item | Rate | Notes |
|---|---|---|
| Model gateway (authoring + solver + reviews) | **~$35-40 per hour** with 30-40 sessions busy | $102 → $203 between 18:14 and 20:50 UTC; no cap |
| AWS compute | ~$21/day on-demand (9) + ~$8/day Spot (8, ~$0.04/h each) | m7i-flex.large ~$0.096/h on-demand |
| AWS other | ~$1.60/day RDS medium, ~$5-7/day IPs, disks, VPC | |
| **AWS total** | **~$35-40 per day** (~$1.5/h) | the model spend is ~25x the AWS spend |

Per task (tonight):
- an accepted task costs **$1.87-$4.84** in model spend (iteration 2: t07 $1.87, t01 $3.10,
  t10 $4.38, t08 v2 $4.84; iter1-t03 v3 $4.05);
- a rejected one $1.90-$9.01 (leak failures after mutations are the most expensive);
- including every task's cost, **~$40 of model spend per accepted task so far** (~$203 for 5
  accepted). Raising the in-band rate is what brings this down.

## How to re-measure

- Limits: `infra/k8s/overlays/aws-prod/kustomization.yaml` and `generation.yaml`; live:
  `scripts/k8s_aws.py status`.
- Node CPU / memory: `kubectl top nodes` (on the server via `scripts/k8s_aws.py`).
- Queue by stage: `GET /v1/queue` (READY = waiting, LEASED = running).
- Spend: `GET /healthz` (`spent_usd`); per task `cost_usd` in `GET /v1/tasks/<id>`; the
  dashboard's Prod view.
- RDS connections: `SELECT count(*) FROM pg_stat_activity` from the control pod.
- AWS cost: Cost Explorer (lags ~1 day).
