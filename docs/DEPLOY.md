# Deployment: local and AWS

Owner: infrastructure session. This covers how the pipeline runs on a laptop and on AWS:
- the adapters;
- the containers;
- Terraform and SSM;
- every runtime setting;
- the guards that keep a runner healthy;
- day-to-day operations.

The load test that set the numbers is in [LOAD_TEST.md](LOAD_TEST.md).

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
| Runners | **8 × `m7i-flex.large`** (2 vCPU / 8 GB), Amazon Linux 2023, 80 GB gp3. The first runner is the **control** runner. First boot (`runner-init.sh.tftpl`) installs Docker, compose 2.32.4 and buildx 0.19.3 |
| Runner access | **SSM only**: no SSH, no inbound ports. IAM role `sde-pipeline-runner`: SSM core, the artifacts bucket, read on `/sde/*` |
| RDS | Postgres 16.13, `db.t4g.micro`, 20 GB, single-AZ, 1-day backups, private. `max_connections` = 79 (§4.4) |
| ElastiCache | Redis 7.1, `cache.t4g.micro`, one node, private |
| S3 | `sde-pipeline-artifacts-284460774261` (private, encrypted): releases, bundles, worlds, traces |
| State | `sde-pipeline-tfstate-284460774261` (made by `aws_ops.py bootstrap`) |
| SSM parameters | see §4.3 |

`compute.tf` ignores changes to `ami`, `user_data` and `associate_public_ip_address`. A new
AMI or init script must never replace a running runner.

**Free-plan limits** (the account is on AWS's Free plan):
- Only `m7i-flex.large`-class instances are allowed: `m6i.2xlarge` was refused.
- The quota is 16 vCPU, so 8 runners. A request for 18 is pending.
- RDS is `db.t4g.micro` only: `db.t4g.small` was refused with `FreeTierRestrictionError`.

Upgrading the plan lifts all three.

| Variable (`variables.tf`) | Default | Meaning |
|---|---|---|
| `runner_count` | 8 | runners (quota-bound) |
| `runner_instance_type` | `m7i-flex.large` | |
| `runner_disk_gb` | 80 | images, builds, the work-dir cache |
| `docker_slots` | **5** | ceiling on Docker jobs per runner (→ `SDE_DOCKER_SLOTS`, a Terraform output passed by `deploy`) |
| `agent_slots` | **2** | ceiling on hour-long agent jobs per runner (→ SSM `/sde/agent_slots`) |
| `agent_lease_ttl_s` | 1800 | an agent job's lease; heartbeats renew it |
| `budget_usd` | 40 | the pipeline's spend cap (→ `/sde/budget_usd`) |
| `db_instance_class` | `db.t4g.micro` | |

Load tests override slots without editing the defaults:
`terraform plan -var docker_slots=8 -var agent_slots=3 -out=…`, then apply that plan.

### 4.2 `deploy/aws_ops.py` (wrapped by `make aws-*`)

Every command first checks that the AWS session is the expected account and region.
Credentials come from `aws login`, exported for Terraform.

| Command | Does |
|---|---|
| `bootstrap` | creates the Terraform state bucket (once) |
| `plan` / `apply` | Terraform |
| `secrets` | copies `OPENROUTER_API_KEY` from `.env` into SSM (`/sde/openrouter_api_key`, SecureString). Generates `/sde/api_key` if missing. Never prints either |
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
| `OPENROUTER_API_BASE` | fixed | `https://gateway.example.com/openrouter/api/v1` | gateway |
| `SDE_API_KEY` | `/sde/api_key` | – | API bearer token |
| `SDE_DOCKER_SLOTS` | Terraform output `docker_slots` | 5 | Docker job ceiling per runner |
| `SDE_AGENT_SLOTS` | `/sde/agent_slots` | 2 | agent job ceiling per runner |
| `SDE_AGENT_LEASE_TTL_S` | `/sde/agent_lease_ttl_s` | 1800 | |
| `SDE_BUDGET_USD` | `/sde/budget_usd` | 40 | spend cap; paid claims stop there |
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

**8 runners is the most the micro instance supports.** Before adding a runner, raising
`SDE_DB_POOL_MAX` or adding a process, do one of these:
- move to `db.t4g.small` (about 180 connections; needs the paid plan);
- make the pools release idle connections.

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
The runners' security group has no inbound rules. Reach the API (`SDE_API_KEY` bearer token
for everything but `/healthz`) through SSM, e.g. port forwarding:
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
| 8 × `m7i-flex.large` | ~$0.10/h each |
| RDS and ElastiCache micro | free tier |
| S3 | pennies |
| Model spend | separate, capped by `SDE_BUDGET_USD` |

**State as of 2026-11-18:** release `b1f0f67` on all 8 runners, **mock mode on**, slots
8 / 3 by Terraform `-var` override, left over from the load test. To return to real runs, see
[LOAD_TEST.md](LOAD_TEST.md) §7.

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
