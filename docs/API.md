# Using the pipeline's API from the command line

The pipeline runs on the AWS K3s cluster ([DEPLOY.md](DEPLOY.md) §8.6). Its API is reachable
from anywhere through a Cloudflare quick tunnel.

## 1. Setup (once per shell)

```bash
# The public URL. It changes whenever the tunnel pod restarts (every deploy), so print the
# current one:
make aws-k8s-public                     # → https://<words>.trycloudflare.com
export SDE_URL=https://<words>.trycloudflare.com

# The API key (an SSM SecureString; needs your AWS login). Don't paste it into chats or files.
export SDE_KEY=$(aws ssm get-parameter --region us-east-2 --name /sde/api_key \
                  --with-decryption --query Parameter.Value --output text)

# A shorthand: every /v1 call needs the X-Api-Key header (not Bearer).
sde() { curl -sS -H "X-Api-Key: $SDE_KEY" -H "Content-Type: application/json" "$@"; }
```

`/healthz` needs no key: `curl -s $SDE_URL/healthz`.

## 2. Check that it's up, and the budget

```bash
curl -s $SDE_URL/healthz | python3 -m json.tool
```

The response carries:
- `status` and `db`;
- the backends (`postgresql`, `RedisQueue`, `S3ArtifactStore`);
- `budget`: `cap_usd`, `spent_usd`, `remaining_usd`, and `budget_exhausted`. When the cap is
  reached, paid jobs stop being claimed.

## 3. Submit work

```bash
# What a request may ask for: the problem families, each with its mechanisms,
# environments, verification methods and shapes.
sde $SDE_URL/v1/families | python3 -m json.tool

# A batch of N tasks (1-50), spread across the taxonomy, or limited to an area. budget_usd caps
# this batch's spend (the global cap still applies). Give batch_id to make it idempotent.
sde -X POST $SDE_URL/v1/batches -d '{
  "batch_id": "night-1", "count": 10,
  "area": ["database_consistency", "compilers_parsers"],
  "dial": 2, "budget_usd": 100
}'
# → {"batch_id": "night-1", "task_ids": [...], "created": true}

# One task: from a free-text idea, an explicit spec, or sampled (no body fields).
sde -X POST $SDE_URL/v1/tasks -d '{"prompt": "a lease-based job queue that double-runs a job after a crash"}'
sde -X POST $SDE_URL/v1/tasks -d '{"spec": {"family": "distributed_concurrency", "dial": 2}}'
sde -X POST $SDE_URL/v1/tasks -d '{}'
```

Request fields:

| Field | Where | Meaning |
|---|---|---|
| `area` | both | one family name or a list (see `/v1/families`) |
| `mechanism`, `environment` | both | narrow within the area |
| `dial` | both | difficulty dial, 1-3 |
| `count` | batch | 1-50 tasks |
| `budget_usd` | batch | the batch's spend cap |
| `prompt` | both | a free-text idea (each task's seed) |
| `spec` | both | an explicit spec (`family`, `dial`, `design`: a list of extra requirements, …) |
| `raw_prompt` | task | the whole user message, verbatim |
| `task_id` / `batch_id` | | your own id (`[a-z0-9-]`, ≤ 64); repeating it returns the existing one |

Give at most one of `prompt`, `spec` and `raw_prompt` per request.

## 4. Watch progress

```bash
sde $SDE_URL/v1/batches | python3 -m json.tool              # every batch: tasks, accepted, rejected, in progress
sde $SDE_URL/v1/batches/night-1 | python3 -m json.tool      # one batch and its tasks
sde $SDE_URL/v1/batches/night-1/report | python3 -m json.tool   # summary.json: yield, cost, time

sde "$SDE_URL/v1/tasks?status=CALIBRATING" | python3 -m json.tool   # tasks, optionally by status / since
sde $SDE_URL/v1/tasks/<task_id> | python3 -m json.tool       # one task: stage, versions, jobs, verdict
sde "$SDE_URL/v1/report?since=2026-11-18T10:00:00Z" | python3 -m json.tool   # yield, cost, time since a moment
```

Each version in `/v1/tasks/<task_id>` has an `analysis` once the Trace Parser has decided it
(null until then):
- `action`, `summary`, `direction`;
- `review`: the pass review (`why_easy`, `simple_strategy`, `fix_applied`, `cues`,
  `suggestions`, `trusted`);
- `mutation`: what the next version mutates (`operators`, their `why`, the `rules` that fired
  with their evidence, `target_fix`);
- `signals`: what those rules read from the passing runs (`files_modified_median`,
  `steps_median`, `cues`).

A task's `status` is one of:
- `CALIBRATING` (in progress);
- `ACCEPTED` (in the 1-3/5 band);
- `REJECTED` (with a reason);
- `CANCELLED`.

## 5. Stop work

```bash
# No further job of the task (or of the batch's undecided tasks) is claimed. Idempotent.
sde -X POST $SDE_URL/v1/tasks/<task_id>/cancel -d '{"by": "james", "note": "wrong area"}'
sde -X POST $SDE_URL/v1/batches/night-1/cancel -d '{"by": "james", "note": "stopping for the night"}'
```

To stop all spending at once, lower the global cap: set SSM `/sde/budget_usd` through
Terraform (`budget_usd`), then `make aws-k8s-deploy OVERLAY=aws-prod`.

## 6. Without the tunnel

The tunnel has no uptime guarantee. The same API is reachable through SSM port forwarding to
the K3s server, which needs your AWS login:

```bash
aws ssm start-session --region us-east-2 --target i-08c25faea3c13a07b \
  --document-name AWS-StartPortForwardingSession \
  --parameters portNumber=30800,localPortNumber=8100
```

This forwards to the API's NodePort, if the overlay exposes one. Otherwise use:

```bash
python deploy/aws_ops.py exec "k3s kubectl -n sde port-forward svc/api 8000:8000"
```

Or run `curl` inside the cluster:

```bash
python scripts/k8s_aws.py status
```
