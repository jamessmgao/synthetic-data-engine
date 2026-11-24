# Deployment: local and AWS

Owner: infrastructure session. This covers how the pipeline runs on a laptop and on AWS:
- the adapters;
- the containers;
- Terraform and SSM;
- every runtime setting;
- the guards that keep a runner healthy;
- day-to-day operations.

The load test that set the numbers is in [LOAD_TEST.md](LOAD_TEST.md).

**Which deployment runs production.** Production runs on K3s across the EC2 instances (§8.6).
§2 and §4 to §6 describe the Docker Compose deployment on the same instances. `make aws-deploy`
still installs it, and it is what `make aws-k8s-down` falls back to. The measurements in §4.4
and §6 were taken on Compose with 8 instances, before the cluster grew; the cluster's current
size is in [CAPACITY.md](CAPACITY.md).

## 1. One codebase, two sets of backends

Every service builds its backends from the environment (`shared/storage/__init__.py`
`from_env`). The same code runs everywhere; only the variables differ:

| Backend | Set by | Unset (plain local) | Local stack (compose.dev) | AWS |
|---|---|---|---|---|
| Store (tasks, jobs) | `DATABASE_URL` | SQLite at `work/sde.db` | Postgres container | RDS Postgres 16 (`db.t4g.micro`) |
| Queue | `REDIS_URL` (+ `SDE_REDIS_PREFIX`, default `sde:`) | the jobs table itself (`DbQueue`) | Redis 7.4 container | ElastiCache Redis 7.1 (`cache.t4g.micro`, one node) |
| Artifacts | `S3_BUCKET` (+ `S3_ENDPOINT_URL`) | files under `work/` | RustFS (S3-compatible) container | S3 `sde-pipeline-artifacts-284460774261` |

**Postgres is the source of truth.** Redis only holds sorted sets of claimable job ids per
type (`sde:<type>`). The reconciler rebuilds them, so losing Redis loses no work.

## 2. The services and where they run

`shared/jobs/supervise.py` runs one container's processes and restarts any that exit. Each
`SDE_*_CMD` names one process; an empty one is skipped.

| Container | Processes (`SDE_*_CMD`) | Local | AWS control runner | AWS other runners |
|---|---|---|---|---|
| `api` | `SDE_API_CMD`: `python -m api` on :8000 | yes | yes | – |
| `worker-control` | `SDE_GENERATION_CMD` (the **one reconciler**; in one-shot mode also the Generation Service), `SDE_TRACE_PARSER_CMD` | yes | yes | – |
| `worker-runner` | `SDE_VALIDATE_CMD` (gates), `SDE_EXECUTOR_CMD` (rollouts), `SDE_RUNNER_GENERATION_CMD` (world / create / revise) | yes | yes | yes |

- **The reconciler runs exactly once**, on `worker-control`. It dead-letters, rebuilds the
  Redis queue, sweeps `.staging` uploads, and handles timeouts.
- **Generation runs on every runner** (since `4c7be83`), `SDE_GENERATION_CONCURRENCY` (8)
  threads each. On the control runner alone, a batch's create jobs once queued for a
  median 21 min.
- **worker-runner mounts the host's Docker socket** and runs as uid 10001 with the socket's
  group (`DOCKER_GID`). Harbor trials, image builds and authoring containers are started by
  the host's Docker daemon. Task containers get no host bind mounts.
- Everything a job starts is labeled `sde.job=<id>` (`SDE_DOCKER_LABEL`) and removed after the
  job.

## 3. Local

```
make test             # infrastructure tests on SQLite + files (no services)
make test-infra       # the same on SQLite, Postgres, Postgres+Redis, plus S3 (starts deps)
make deps-up          # Postgres + Redis + RustFS only
make up               # the whole stack, one-shot authoring
make up-agent         # the whole stack, agent authoring (builds the authoring image first)
make down | logs | ps | reconcile
```

**Settings: `deploy/local.env`.** It's gitignored; copy `deploy/local.env.example`. Every app
service reads it, the same way prod reads `runtime.env`. So any setting in it reaches every
container: budget cap, slots, author mode, guards, mock mode.
- `make up-agent` writes `SDE_AUTHOR_MODE=agent` to `deploy/make.env`, which is read *before*
  `local.env`, so your file wins. `make up` deletes `make.env`.
- No `environment:` entry in `compose.dev.yaml` may set an `SDE_*` setting. Compose lets
  `environment:` beat `env_file`, even with an empty `${VAR:-}`. Until `b31ff23` / `12d890d`,
  only three settings reached the containers, so a local stack ran with **no budget cap**.
- `tests/deploy/test_compose_env.py` fails in these cases:
  - the code reads a setting that `local.env.example` doesn't list;
  - a service stops reading the env files;
  - an `environment:` entry shadows a setting.

The local work dir is `$PWD/work`, mounted at the same path in the containers. Harbor's task
containers are started by the host's daemon, so paths must match on both sides.

## 4. AWS

### 4.1 What Terraform creates (`deploy/terraform/`)

Account `284460774261` (`data-capture-platform`), region `us-east-2`. Everything is named
`sde-pipeline-*` and tagged with project `sde-task-pipeline`, so nothing existing in the account
is touched.

| Resource | Details |
|---|---|
| VPC `10.42.0.0/16` | 2 public subnets (runners), 2 private (RDS, Redis); internet gateway; no NAT |
| Runners | **17 × `m7i-flex.large`** (2 vCPU / 8 GB) by default: 9 on-demand and 8 Spot (`runner_count`, `on_demand_count`). Amazon Linux 2023, 80 GB gp3. The first runner is the **control** runner. First boot (`runner-init.sh.tftpl`) installs Docker, compose 2.32.4 and buildx 0.19.3 |
| Runner access | **SSM only**: no SSH, no inbound ports from outside (the instances reach each other, for K3s). IAM role `sde-pipeline-runner`: SSM core, the artifacts bucket, read on `/sde/*` |
| RDS | Postgres 16.13, `db.t4g.medium` by default (`db_instance_class`), 20 GB, single-AZ, 1-day backups, private. It was `db.t4g.micro` (`max_connections` = 79) when §4.4 was measured |
| ElastiCache | Redis 7.1, `cache.t4g.micro`, one node, private |
| S3 | `sde-pipeline-artifacts-284460774261` (private, encrypted): releases, bundles, worlds, traces |
| State | `sde-pipeline-tfstate-284460774261` (made by `aws_ops.py bootstrap`) |
| SSM parameters | see §4.3 |

`compute.tf` ignores changes to `ami`, `user_data` and `associate_public_ip_address`. A new
AMI or init script must never replace a running runner.

**Free-plan limits.** The account started on AWS's Free plan, which set the first sizes:
- Only `m7i-flex.large`-class instances were allowed: `m6i.2xlarge` was refused.
- The quota was 16 vCPU, so 8 instances.
- RDS was `db.t4g.micro` only: `db.t4g.small` was refused with `FreeTierRestrictionError`.

The upgrade to the Paid plan (2026-11-18) lifted the Spot and RDS limits. The defaults in
`variables.tf` are now 17 instances (9 on-demand, the on-demand quota of 18 vCPU, plus 8 Spot)
and `db.t4g.medium`.

| Variable (`variables.tf`) | Default | Meaning |
|---|---|---|
| `runner_count` | 17 | EC2 instances; the first `on_demand_count` are on-demand, the rest Spot |
| `on_demand_count` | 9 | on-demand instances (9 × 2 vCPU is the on-demand quota of 18 vCPU) |
| `runner_instance_type` | `m7i-flex.large` | |
| `runner_disk_gb` | 80 | images, builds, the work-dir cache |
| `docker_slots` | **5** | ceiling on Docker jobs per runner (→ `SDE_DOCKER_SLOTS`, a Terraform output passed by `deploy`) |
| `agent_slots` | **2** | ceiling on hour-long agent jobs per runner (→ SSM `/sde/agent_slots`) |
| `agent_lease_ttl_s` | 1800 | an agent job's lease; heartbeats renew it |
| `budget_usd` | `none` | the pipeline's spend cap (→ `/sde/budget_usd`); `none` means no cap |
| `db_instance_class` | `db.t4g.medium` | changing it modifies the database in place |

Load tests override slots without editing the defaults:
`terraform plan -var docker_slots=8 -var agent_slots=3 -out=…`, then apply that plan.

### 4.2 `deploy/aws_ops.py` (wrapped by `make aws-*`)

Every command first checks that the AWS session is the expected account and region.
Credentials come from `aws login`, exported for Terraform.

| Command | Does |
|---|---|
| `bootstrap` | creates the Terraform state bucket (once) |
| `plan` / `apply` | Terraform |
| `secrets` | copies `OPENROUTER_API_KEY` from `.env` into SSM (`/sde/openrouter_api_key`, SecureString) and `OPENROUTER_API_BASE` into `/sde/openrouter_api_base`. Generates `/sde/api_key` if missing. Never prints a key |
| `author-mode oneshot\|agent` | sets `/sde/author_mode` (takes effect on the next deploy) |
| `deploy [REF]` | `git archive` of REF → S3 `releases/<sha>.tar.gz`, then on every runner in parallel, via SSM: `remote-deploy.sh <sha> <bucket> <region> <docker_slots> <control\|runner>` |
| `smoke` | `shared.jobs.smoke` on the control runner: queue + artifacts round trip against RDS / Redis / S3, plus one reconcile pass |
| `status` | containers and deployed release per runner |
| `logs [SERVICE]` | the control runner's last 100 log lines |
| `exec CMD…` | a shell command on every runner |
| `stop` / `start` | stop or start the runners (RDS / Redis / S3 keep running) |
| `destroy` | Terraform destroy |

`make aws-up` = bootstrap → apply → secrets → deploy → smoke.

**`remote-deploy.sh`** (on the runner):
1. Unpacks the release once into `/opt/sde/releases/<sha>`, renamed into place, and points
   `/opt/sde/current` at it.
2. Builds the authoring image `sde-authoring:<hash of authoring/ config/ generator/
   validator/>` only when that content changed.
3. Writes `/opt/sde/runtime.env` from SSM (§4.3).
4. Gives uid 10001 ownership of `/opt/sde/work` and `/opt/sde/output`.
5. Runs `docker compose -f deploy/compose.prod.yaml build --quiet && up -d`. On non-control
   runners it builds and starts only `worker-runner`.

SSM keeps only the first 24,000 characters of output, hence the quiet build.

### 4.3 `runtime.env`: every setting, and where it comes from

`compose.prod.yaml` passes the **whole file** to every container (`env_file`), so nothing
can be silently left out.

| Setting | Source | Default | Meaning |
|---|---|---|---|
| `DATABASE_URL`, `REDIS_URL`, `S3_BUCKET` | `/sde/database_url`, `/sde/redis_url`, `/sde/s3_bucket` (Terraform) | – | backends |
| `OPENROUTER_API_KEY` | `/sde/openrouter_api_key` (`secrets`) | – | inference gateway key |
| `OPENROUTER_API_BASE` | `/sde/openrouter_api_base` (`secrets`) | – | inference gateway base URL; the deploy fails if the parameter is missing |
| `SDE_API_KEY` | `/sde/api_key` | – | the API key, sent as the `X-Api-Key` header |
| `SDE_DOCKER_SLOTS` | Terraform output `docker_slots` | 5 | Docker job ceiling per runner |
| `SDE_AGENT_SLOTS` | `/sde/agent_slots` | 2 | agent job ceiling per runner |
| `SDE_AGENT_LEASE_TTL_S` | `/sde/agent_lease_ttl_s` | 1800 | |
| `SDE_BUDGET_USD` | `/sde/budget_usd` | `none` from Terraform (40 if the parameter is missing) | spend cap; paid claims stop there |
| `SDE_AUTHOR_MODE` | `/sde/author_mode` | `oneshot` | `agent` = authoring trials in the authoring image |
| `SDE_AUTHORING_IMAGE` | built by the deploy | `sde-authoring:<hash>` | |
| `SDE_GENERATION_CONCURRENCY` | `/sde/generation_concurrency` | 8 | generation threads per runner |
| `SDE_DB_POOL_MAX` | `/sde/db_pool_max` | 2 | main pool per process (§4.4) |
| `SDE_DOCKER_CLAIM_SPACING_S` | `/sde/docker_claim_spacing_s` | 10 | claim pacing (§5) |
| `SDE_MOCK_LLM`, `SDE_MOCK_LLM_S`, `SDE_MOCK_ROLLOUT_S`, `SDE_MOCK_PASS_P` | `/sde/mock_llm` (0), `/sde/mock_llm_s` (3), `/sde/mock_rollout_s` (30), `/sde/mock_pass_p` (0.4) | off | load-test mode ([LOAD_TEST.md](LOAD_TEST.md) §2) |
| `SDE_WORK_DIR` | fixed | `/opt/sde/work` | |
| `SDE_RELEASE` | the deploy | `<sha>` | shown in `/healthz` and smoke |
| `DOCKER_GID` | the host's socket | | |
| `SDE_*_CMD` | the deploy | per role (§2) | |

Settings not listed take the code's default; `deploy/local.env.example` lists them all. An
optional parameter is read with `param_or`, so a missing one falls back to the default above.
Change a value with:

```
aws ssm put-parameter --region us-east-2 --name /sde/<name> --type String --value <v> --overwrite
```

Then redeploy. The Terraform-managed parameters (`agent_slots`, `agent_lease_ttl_s`,
`budget_usd`) are changed through Terraform variables instead.

### 4.4 Database connections: the limit on scaling out

The numbers below were measured on Compose with 8 instances and `db.t4g.micro`. The pools work
the same way on K3s; the database is now `db.t4g.medium`, and its connection use is in
[CAPACITY.md](CAPACITY.md).

- Each process holds **one bounded psycopg pool**: `SDE_DB_POOL_MAX`, 2 on AWS. Waits of
  `SDE_DB_POOL_WARN_S` (2 s) or more are logged as `waited … for a database connection`.
- Each process also holds a **dedicated 1-connection heartbeat pool**, so lease renewals
  never queue behind busy queries (`5f86caa`).
- `application_name` is `sde-pipeline` or `sde-pipeline-heartbeat`, so `pg_stat_activity`
  shows which is which.
- Before the pools existed, one connection per thread exhausted the micro instance
  (`remaining connection slots are reserved for rds_reserved`). Heartbeats failed, leases
  expired, and running jobs were treated as abandoned.

```
per process   ≤ 2 main + 1 heartbeat = 3
per runner    3 processes            ≤ 9      control adds 3 processes ≤ 9
db.t4g.micro  max_connections 79, ~3 reserved → 76 usable
measured      66 with 8 runners (load test, mid-run)
```

**8 instances on Compose was the most the micro instance supported.** That is why the database
moved to `db.t4g.medium` before the cluster grew. Before adding instances, raising
`SDE_DB_POOL_MAX` or adding a process, check the connection count against `max_connections`
again.

## 5. Keeping a runner healthy: slots, guards, pacing, leases

The claim loop (`shared/jobs/jobqueue.py` `JobQueue.claim` / `_unblocked`, `shared/storage/sql.py`
`claim`) decides whether a process may take one more job.

| Mechanism | Setting | What it does |
|---|---|---|
| Docker slots | `SDE_DOCKER_SLOTS` (5) | jobs with `needs_docker` on this machine, counted by owner prefix `host:pid:`, across all of its processes |
| Agent slots | `SDE_AGENT_SLOTS` (2) | among them, hour-long agent jobs (world, create / revise in agent mode) |
| Memory guard | `SDE_MIN_FREE_MEMORY_PCT` (20) | below 20% `MemAvailable`, no new Docker work (cap 0); running jobs are untouched |
| CPU guard | `SDE_MAX_CPU_LOAD_PCT` (90) | at ≥ 90% of the 1-minute load average per CPU (`/proc/loadavg`: the host's, even inside a container; no guard off Linux), no new Docker work. A load average, not instant CPU %, which image builds make too bursty. Added after 8 rollouts on 2 vCPU timed out terminus-2's `tmux send-keys` |
| Guard logs | – | `guard: memory` / `guard: cpu`, at most once a minute per process |
| Claim pacing | `SDE_DOCKER_CLAIM_SPACING_S` (code 0, AWS 10) | a process waits this long between Docker claims. The load average lags a burst by a minute, so without pacing the first runner to poll took 8 builds at once while others sat idle ([LOAD_TEST.md](LOAD_TEST.md) §6.2, §6.4) |
| Leases | `SDE_LEASE_TTL_S`, `SDE_AGENT_LEASE_TTL_S`, `SDE_HEARTBEAT_S` | a job is leased; heartbeats renew it; `lease_token` fences a stale worker's settle |
| Crash isolation | – | a job whose lease expired (a *suspect*: its worker may have crashed it) goes to a process holding no other lease, so a second crash is charged to it alone (`isolated = 1`). `MAX_JOB_ATTEMPTS` dead-letters a real poison job |
| Suspect wait cap | `SDE_SUSPECT_MAX_WAIT_S` (600) | after that long past expiry, any process may take the suspect, still isolated. Before `f743fd1`, an executor always held a lease, so abandoned rollouts waited hours |
| Budget | `SDE_BUDGET_USD` | paid job types stop claiming at the cap, and say so in the logs and `/healthz` |

Running jobs never lose their slot to a guard; the guards only stop new claims.

## 6. Operations

**Health:**
```
python deploy/aws_ops.py status        # containers + release per runner
python deploy/aws_ops.py smoke         # backends round trip
python deploy/aws_ops.py exec "curl -s localhost:8000/healthz"   # backends, release, budget (control runner answers)
python deploy/aws_ops.py exec "cd /opt/sde/current && docker compose --env-file /opt/sde/runtime.env \
  -f deploy/compose.prod.yaml exec -T worker-runner env | grep ^SDE_"   # effective settings
```
The instances' security group lets nothing in from outside. On Compose, reach the API (the
`X-Api-Key: <SDE_API_KEY>` header for everything but `/healthz`) through SSM, e.g. port forwarding:
`aws ssm start-session --target <control id> --document-name AWS-StartPortForwardingSession
--parameters portNumber=8000,localPortNumber=8000`.

**Change concurrency or the budget:**
1. Edit the Terraform variable, or pass `-var`.
2. `apply`.
3. `deploy`.

**Switch author mode:** `author-mode agent`, then `deploy`.

**Stop spending:** `aws_ops.py stop` stops the runners (the main cost). RDS, Redis and S3 keep
their state.

**Load-test mode on and off:**
1. Set `/sde/mock_llm` to `1` (or `0`).
2. `deploy`.

Never turn mock mode off while mock tasks are still running: their remaining rollouts would
call the real model. Clear the fixture's cached leak review (`/opt/sde/work/leak_reviews/<sha>`
on each runner) after changing the mock ([LOAD_TEST.md](LOAD_TEST.md) §6.6).

**A release is immutable.** `deploy` of the same sha reuses the unpacked tree. Rolling back
means deploying an older sha.

**Costs (us-east-2, on demand, approximate):**

| Item | Cost |
|---|---|
| `m7i-flex.large` | ~$0.10/h each on demand |
| RDS and ElastiCache | free tier while both were micro; the current rates are in [CAPACITY.md](CAPACITY.md) |
| S3 | pennies |
| Model spend | separate, capped by `SDE_BUDGET_USD` |

**State after the first load test (2026-11-18):** release `b1f0f67` on all 8 instances, on
Compose, **mock mode on**, slots 8 / 3 by Terraform `-var` override. [LOAD_TEST.md](LOAD_TEST.md)
§7 covers returning to real runs. The production cluster built after that is in
[CAPACITY.md](CAPACITY.md).

## 7. Files

| Path | Role |
|---|---|
| `deploy/Dockerfile` | the app image (api, workers) |
| `deploy/compose.dev.yaml` | local stack: Postgres, Redis, RustFS, app services (profile `app`) |
| `deploy/compose.prod.yaml` | AWS: app services only, `env_file: /opt/sde/runtime.env`, Docker socket on worker-runner |
| `deploy/local.env.example` | every setting (copy to `deploy/local.env`) |
| `deploy/terraform/` | AWS resources (§4.1) |
| `deploy/aws_ops.py` | operations (§4.2) |
| `deploy/remote-deploy.sh` | the per-runner deploy (§4.2) |
| `authoring/Dockerfile` | the agent authoring image (Python 3.12, Go 1.22, Temurin 21, gcc, sqlite, psql, docker CLI, pytest, harbor 0.26, `sde-world` / `sde-task`) |
| `shared/jobs/supervise.py` | per-container process supervisor |
| `scripts/inject_task.py`, `scripts/loadtest.py` | test past generation; load test |
| `deploy/k8s/` | Kubernetes manifests: the base and the `local`, `aws`, `aws-prod` and `aws-prod-mock` overlays (§8) |
| `deploy/k8s-local.sh` | the local k3d cluster (§8.4) |
| `deploy/k8s_aws.py` | K3s on the EC2 instances (§8.6) |
| `scripts/k8s_test.py` | the Kubernetes chaos test (§8.4) |

## 8. Kubernetes (k3d locally, K3s on AWS)

The pipeline also runs on a local K3s cluster: k3d cluster `sde`, kube context `k3d-sde`,
namespace `sde`. The plan, and the reasons for each choice, are in [KUBERNETES.md](KUBERNETES.md).
It started as a local deployment (§8.1 to §8.4). The same manifests now run production on the
AWS EC2 instances (§8.6).

### 8.1 What runs where

| Object | Kind | Replicas | Runs |
|---|---|---|---|
| `api` | Deployment | 1 | `python -m api` |
| `control` | Deployment | **exactly 1** | the reconciler and the Trace Parser (the reconciler must run once) |
| `validate` | Deployment | 1-2 | `python -m validator.service validate`, plus a `dind` sidecar |
| `executor` | Deployment | 1-4 (KEDA) | `python -m evaluation.executor`, plus a `dind` sidecar |
| `generation` | Deployment | 1 | `python -m generator.service generation`, plus a `dind` sidecar |
| `postgres`, `redis`, `s3` | StatefulSet | 1 each | the backends of §1, on local-path volumes |
| bootstrap | Job | once per deploy | schema and bucket; the app pods wait for it |

- **One image**, `sde-pipeline:local`, built from `deploy/Dockerfile`, the same image compose uses.
- **Settings:** ConfigMap `sde-config` and Secret `sde-secrets`, generated by kustomize from
  `deploy/local.env`, with the same `SDE_*` names as §4.3.
- **Docker slots are per pod.** Slots are counted per container hostname, which is the pod
  name, so `SDE_DOCKER_SLOTS` applies per pod with no code change. The memory and CPU guards
  still read the whole node's `/proc`.

### 8.2 Docker: a sidecar per worker pod (variant B)

Harbor's own Kubernetes backend needs GKE, so worker pods run their trials with Docker. Each
pod that runs Docker work has a privileged `docker:dind` sidecar:
- the worker reaches it at `DOCKER_HOST=unix:///var/run/dind/docker.sock` (a shared volume);
- both containers mount the work dir as an `emptyDir` at the same path, `/opt/sde/work`,
  because Harbor passes host paths to `docker run -v`.

**Why the sidecar, and not the Mac's Docker socket:** with the sidecar, a trial's CPU and
memory count against its pod's limits, so Kubernetes sees and schedules the real load. With
the Mac's socket (what `compose.dev` does), trials run outside Kubernetes and the cluster
can't see them.

What it costs:
- privileged pods;
- each new pod starts with a cold image cache, so its first build takes about 1 min;
- killing a pod kills its sidecar, and every trial in it with it. That's the right
  outcome: the lease expires, and another pod reruns the rollout (§5).

### 8.3 Autoscaling (KEDA)

`deploy/k8s/base/keda.yaml` holds a ScaledObject on `executor` that uses the postgresql
scaler:
- the metric is outstanding rollout work in the `jobs` table: READY + LEASED rollouts.
  Counting LEASED means a scale-down never removes a pod that is still running a rollout
  while the queue is empty;
- the target is 4 per replica (= `SDE_DOCKER_SLOTS`), from 1 replica up to 4;
- it scales down one pod per minute, after a 2-minute window. The executor has a 60 s
  termination grace period and releases its leases on SIGTERM.

KEDA reads the database through a TriggerAuthentication that points at `sde-secrets`
(`DATABASE_URL`). `keda-kustomizeconfig.yaml` lets kustomize rewrite that reference to the
hash-suffixed Secret name.

KEDA itself is installed once per cluster, from its pinned release manifest. It isn't in
the kustomization, because it lives in its own `keda` namespace:

```bash
kubectl --context k3d-sde apply --server-side \
  -f https://github.com/kedacore/keda/releases/download/v2.16.1/keda-2.16.1.yaml
kubectl --context k3d-sde -n keda rollout status deploy/keda-operator
```

### 8.4 Commands

Always pass `--context k3d-sde`: the scripts never switch your current context.

```bash
make k8s-up                 # k3d cluster sde: 1 server + 2 agents (idempotent)
make k8s-deploy             # build sde-pipeline:local, import it (checked with crictl on every node), apply -k, rerun bootstrap
make k8s-status             # pods, Deployments, StatefulSets, the ScaledObject
make k8s-logs SVC=executor
make k8s-test               # the chaos test below (mock mode); ARGS="--tasks 10" for a burst
make k8s-down
kubectl kustomize deploy/k8s/overlays/local   # exactly what gets applied
```

`make k8s-test` (`scripts/k8s_test.py`) runs against a stack deployed with `SDE_MOCK_LLM=1`:
1. It injects 2 mock tasks (`scripts/inject_task.py` in the `control` pod).
2. It force-deletes (grace 0) the executor pod that holds a LEASED rollout.
3. It restarts `postgres-0` while those tasks are still open.
4. When every task is decided, it checks:
   - every rollout slot was scored at most once, and every analyzed version has 5 scored slots;
   - the killed rollout was re-claimed (its `lease_token` went up) and settled once;
   - no job is left LEASED or READY;
   - no `sde.job=*` container is left, and nothing is running, in any `dind` sidecar;
   - KEDA scaled `executor` above 1 replica during the run. The test samples
     `.status.replicas` every 10 s; 2 tasks are 10 rollouts, more than one executor's
     target of 4. A scale-down is reported if it happens before the run ends.

`--dry-run` prints every kubectl command it would run, without touching the cluster.

A killed pod's rollout is re-claimed only after its lease expires. The local overlay
shortens the leases for this (`SDE_LEASE_TTL_S=45`, `SDE_HEARTBEAT_S=15`,
`SDE_SUSPECT_MAX_WAIT_S=10`; production keeps 600 / 600), so that takes about 1 minute. It
also shortens the mock waits (`SDE_MOCK_ROLLOUT_S=15`, `SDE_MOCK_LLM_S=0.5`), so a run takes a
few minutes, mostly image builds.

**Measured** (2026-11-18, on the AWS K3s cluster, §8.6, with the test overlay `aws`: 8 EC2
instances, in-cluster data stores, mock mode). Two runs of `make aws-k8s-test`, releases
`63863db` and `ae2dd90`:

| Check | Run 1 | Run 2 |
|---|---|---|
| Preflight: 5 Deployments + 3 StatefulSets ready, mock on | ✅ | ✅ |
| Executor pod force-deleted while holding a LEASED rollout | ✅ | ✅ |
| `postgres-0` restarted with tasks open; tasks survived | ✅ | ✅ |
| Both tasks decided | ✅ 311 s, both ACCEPTED | ✅ 297 s, both ACCEPTED |
| No rollout slot scored twice; 5 scored slots per version | ✅ | ✅ |
| The killed rollout re-claimed and settled once (`lease_token` 1 → 2, attempts 2) | ✅ | ✅ |
| No job left LEASED or READY | ✅ | ✅ |
| Leak check in the dind sidecars | ❌ test bug (below) | ❌ test bug (below) |
| KEDA scale-up summary | not reached | not reached |

The leak check failed both times with "0 pods with a dind sidecar", a bug in the test, not
the cluster:
- **Run 1:** it looked for `dind` in `containers`, but dind is a native sidecar, listed in
  `initContainers` (fixed in `ae2dd90`).
- **Run 2:** it selected pods by `app.kubernetes.io/part-of`, which only the Deployments
  carry, not their pod templates (fixed in `f39e214`: it selects by
  `app.kubernetes.io/name`).

The fixed leak check and the KEDA summary haven't run yet.

**Production check** (`aws-prod-mock`, §8.6): RDS, ElastiCache and S3, with no model
calls. Run on 2026-11-18, release `37ca6ae`:
- deploy 209 s;
- a pod reaches S3 through the instance role (`assumed-role/sde-pipeline-runner/…`);
- all 6 generation pods loaded the authoring image (`3/3`);
- `shared.jobs.smoke` ok on RDS + ElastiCache + S3.

The injected fixture task `prodmock-001` went through validate, 5 rollouts and analysis to
**ACCEPTED / BAND_OK** in 4.5 min:
- validate OK;
- rollouts 2 VALID_PASS / 3 VALID_FAIL / 0 INVALID;
- $0.00.

That first deploy found two bugs in aws-prod, both fixed in `37ca6ae`:
- no image tag (ErrImageNeverPull);
- the authoring-image loader ordered before dind (a deadlock).

### 8.5 Local vs AWS

The local k3d cluster (§8.1-8.4) proved the manifests and the dind sidecar. Testing then
moved to AWS (§8.6), because the Mac is reserved for task generation. When that was decided
(2026-11-18, on the Free plan), K3s didn't raise throughput: the EC2 instances were a fixed
16 vCPU, the work is CPU-bound, and RDS micro capped the processes (§4.4).

What K3s adds on AWS is **isolation**. An authoring agent reaches only its own generation
pod's dind. Tonight an agent ran `docker kill $(docker ps -q)` on the Mac and killed every
container on the machine. On compose, the same command would kill everything on an
instance; on K3s it kills only that pod's trials (up to 3 authoring sessions per pod in
aws-prod, `SDE_AGENT_SLOTS` in the overlay's `generation.yaml`). dind is privileged, so this contains accidents, not a determined escape.

### 8.6 K3s on the AWS EC2 instances (`deploy/k8s_aws.py`)

The same manifests run on the EC2 instances as one K3s cluster: a server
(`aws_ops.runners()[0]`) and every other instance as an agent, installed over SSM. It started
on 8 instances; Terraform's default is now 17 (§4.1), and new ones join with
`deploy/k8s_aws.py join`. There are no new AWS resources beyond one security-group rule
(instance to instance, `2976f6d`).

| Command | Does |
|---|---|
| `make aws-k8s-up` | stops compose on every instance; installs K3s v1.35.5 (pod CIDR `10.52.0.0/16`, service CIDR `10.53.0.0/16`: the VPC is `10.42.0.0/16`, K3s's default pod range, and the overlap kept agents NotReady); builds `sde-pipeline:local` on every instance and imports it, with a crictl check; installs KEDA v2.16.1. The join token is SSM SecureString `/sde/k3s_token` |
| `make aws-k8s-deploy OVERLAY=aws\|aws-prod\|aws-prod-mock` | ships the release, rebuilds and imports, applies the overlay, waits for the rollouts. For the production overlays it also creates Secret `sde-secrets` from SSM (values never in command text), builds the authoring image on every instance (saved to `/opt/sde/images/sde-authoring.tar`, loaded into each generation pod's dind), and checks IMDS from a pod |
| `make aws-k8s-status` / `aws-k8s-logs SVC=…` | nodes, pods, services / logs |
| `make aws-k8s-test ARGS=…` / `aws-k8s-test-log` | the chaos test on the server, as systemd unit `sde-k8s-test` (an SSM timeout kills a nohup'd process) |
| `make aws-k8s-public` | the public HTTPS links of the API and the dashboard: Cloudflare quick tunnels (`public-tunnel` and `dashboard-tunnel` Deployments, dialing out, no inbound ports). Anyone with the URL reaches the API; everything but `/healthz` needs the `X-Api-Key: <SDE_API_KEY>` header. A URL changes when its tunnel pod restarts |
| `make aws-k8s-down` | uninstalls K3s; then `make aws-deploy` restores compose |

| Overlay | Data | Models | Notes |
|---|---|---|---|
| `aws` | in-cluster Postgres / Redis / RustFS on the server | mock | the chaos test's overlay; short leases |
| `aws-prod` | RDS, ElastiCache, S3 (via the instance role) | real | agent authoring; 14 generation pods × 3 sessions, validate ×4, executor 2-16 via KEDA with 2 agent runs per pod; the dashboard and both tunnels. The overlay's `kustomization.yaml` and `generation.yaml` are the source for these numbers |
| `aws-prod-mock` | as aws-prod | mock, and `SDE_AGENT_SLOTS=0` on generation (mock mode doesn't cover authoring trials) | a production check with no model calls |
