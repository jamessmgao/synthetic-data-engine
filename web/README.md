# Dashboard

A read-only UI over the pipeline API: what's running, how every batch and task is doing,
and the results behind them (task bundles, worlds, rollout traces). Next.js 16 + Tailwind 4 +
react-query, set up like `expert-capture-platform/web`. Every view refreshes every 10 s.

```bash
make ui-aws      # the AWS cluster: writes web/.env.local (tunnel URL + SSM key), runs the UI
make ui          # whatever web/.env.local points at, e.g. `make up` → SDE_URL=http://localhost:8000
open http://localhost:3100
```

On the AWS cluster (aws-prod overlays) it also runs as a pod with its own public link, no
password, read-only: `make aws-k8s-public` prints `api: …` and `dashboard: …`. UI changes ship
alone with `make aws-k8s-deploy-ui` (only the `dashboard` pod restarts; both URLs stay).
`make aws-k8s-deploy OVERLAY=aws-prod ONLY=api,control` scopes a pipeline deploy the same way.

`web/.env.local` (see `.env.example`) holds `SDE_URL` and `SDE_API_KEY`. The browser calls
`/api/sde/<path>`; `app/api/sde/[...path]/route.ts` forwards GETs only and adds the key on the
server, so the key never reaches the browser and the UI can't change anything. The tunnel
URL changes on every deploy: re-run `make ui-aws`.

| Page | Shows | API |
|---|---|---|
| `/` | totals, yield and cost; jobs by type × status; jobs running now; recent batches | `/v1/report`, `/v1/queue`, `/v1/batches` |
| `/batches/<id>` | the batch report (yield, cost, pass-count histogram, per family) and its tasks | `/v1/batches/<id>`, `…/report` |
| `/tasks` | every task, by status, searchable | `/v1/tasks` |
| `/tasks/<id>` | the decision; each version's origin, gates, world and rollouts (failure explanation, blame, failing tests, tool calls); every job | `/v1/tasks/<id>` |
| `/artifacts/<bundles\|worlds\|traces>/<key>` | the files of a bundle, world or trace | `/v1/artifacts/…` |

`/v1/queue`, `/v1/artifacts/…` and the task's `jobs` list were added for this UI
(`api/service.py`); an API deployed before them shows an error in those panels only.
