---
name: sde-pipeline-api
description: Drive the synthetic-data-engine pipeline's HTTP API (the Agentic RL Environment Generation Platform): check health and spend, submit batches of new tasks across the six families, watch queues and tasks, read accepted tasks and failure explanations, and cancel stuck work. Use when asked to check the jobs, top up the queue, submit or cancel tasks or batches, report yield or cost, or look up a task, batch, world or agent run.
---

# Driving the pipeline's API

The pipeline (the repo README has the overview) generates Terminal-Bench tasks and keeps the ones the
pinned solver passes 1-3 of 5 times. Everything is a call to one HTTP API. No code changes are
needed to operate it.

## Setup (once per shell)

```bash
SDE_URL=$(make -s aws-k8s-public | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1)  # changes on every deploy
SDE_KEY=$(aws ssm get-parameter --region us-east-2 --name /sde/api_key --with-decryption --query Parameter.Value --output text)
sde() { curl -sS -m 60 -H "X-Api-Key: $SDE_KEY" -H 'Content-Type: application/json' "$@"; }   # X-Api-Key, not Bearer
```

**Never print or paste the API key or the gateway key** (`/sde/openrouter_api_key`). Locally the API is
`http://localhost:8000` with `SDE_API_KEY` from `deploy/local.env`.

## Routes

| Need | Call |
|---|---|
| Health and spend | `GET /healthz` (no key): `budget.spent_usd`, `leased_estimate_usd` |
| What is running and waiting | `GET /v1/queue`: `counts[type]` = `{LEASED, READY, DONE, DEAD}`, `running[]` |
| Tasks | `GET /v1/tasks?status=&since=&scope=prod`; each has `status`, `stage`, `pass_pattern`, `family`, `mechanism`, `accepted_version`, `versions[{n, origin, pass_count}]` |
| One task in full | `GET /v1/tasks/{id}`: versions, gates, leak reviews, agent runs with `failure.explanation`, jobs, cost |
| Yield and cost | `GET /v1/report?scope=prod` (by family and mechanism); `GET /v1/batches/{id}/report` |
| Batches | `GET /v1/batches`, `GET /v1/batches/{id}` |
| What can be requested | `GET /v1/families`; `GET /v1/worlds?area=` (the reusable world pool) |
| Files of a bundle, world or trace | `GET /v1/artifacts/{kind}/{key}` and `.../file?path=` |
| Submit | `POST /v1/batches` `{batch_id, count 1-50, area, mechanism, environment, dial, budget_usd, reuse_worlds}`; `POST /v1/tasks` |
| Cancel | `POST /v1/tasks/{id}/cancel`, `POST /v1/batches/{id}/cancel` with `{"by", "note"}`; idempotent |

`scope` is `prod` (real runs), `dev` (mock runs) or `all`. Reuse the same `batch_id` to make a
submit idempotent. Full shapes: `docs/API.md`.

## Recipes

**Check the jobs** (a few lines of output):
```bash
curl -s $SDE_URL/healthz
sde $SDE_URL/v1/queue | python3 -c "import json,sys;d=json.load(sys.stdin)['counts'];print({t:(c.get('LEASED',0),c.get('READY',0)) for t,c in d.items() if c.get('LEASED') or c.get('READY')})"
sde "$SDE_URL/v1/tasks" | python3 -c "import json,sys,collections;t=[x for x in json.load(sys.stdin) if not x['dev']];print(collections.Counter(x['status'] for x in t));print(collections.Counter(x.get('family') for x in t if x['status'] in ('CREATED','CALIBRATING')))"
```
Report: time, spend, running/waiting per job type, prod tasks by status, in flight by family, and errors on running jobs.

**Top up, only when the queue is short** (few agent runs or worlds waiting; authoring has 42 sessions and agent runs 2-16 executor pods, so a long `rollout` queue means do not add work):
```bash
sde -X POST $SDE_URL/v1/batches -d '{"batch_id":"aws-w9-l2-0339","count":4,"area":["compilers_parsers","database_consistency"],"dial":3}'
```
- Spread `area` across the **least-covered families** (count in-flight by family first).
- Default to **new worlds** (no `reuse_worlds`) at `dial` 3. Use `reuse_worlds: true` only when told to.
- A new batch id every time (`aws-w<window>-<lane>-<hhmm>`); 4-5 tasks per top-up.

**Look into a task:** `GET /v1/tasks/{id}`; read each version's `pass_pattern`, then each agent run's
`failure.explanation` (the Trace Parser's attribution, quoted as given), then the trace with
`GET /v1/artifacts/traces/{job_id}/file?path=...` (job id = `<task>-v<N>-rollout<slot>-r0`).

**Cancel** only a task stuck over ~90 minutes with the same error twice; use a `note`.

## Rules

- Say **"agent runs"** for solver runs (the code and job type keep the name `rollout`).
- Take every Trace Parser failure attribution as given; don't dispute it.
- A timeout is not a failure by itself: the Trace Parser rules on it.
- If a call fails with **"API key spend limit exceeded"** (the gateway key ran out), stop and ask before
  switching to the backup key. Never print either key.
- No Terraform, node or pod-count changes, or `make aws-k8s-down` without the user's explicit OK.
- Be frugal: the ledger spend is roughly twice the gateway's own figure; check `/healthz` before a
  big submit.
