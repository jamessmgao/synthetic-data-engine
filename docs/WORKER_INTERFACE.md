# Task builder: interface for the Generation Service and Build + Validate

The task builder implements two of the PRD's job types as plain functions. They
hold no database state: a worker loop (`shared/jobs/worker.py`) leases the job row,
calls the function, and records what it returns. Paths are relative to the
artifact store, `config.WORK_DIR` (default `./work`, override with `SDE_WORK_DIR`).

**Hand-offs (docs/PRD.md §5, adopted 2026-11-17).** The worker that settles a job
also enqueues the next stage in the same transaction:

- a finished `create` or `revise` → its `validate` job;
- `validate` OK → five `rollout` jobs on the run queue;
- `validate` FAILED → a `repair` on the revise queue (≤ `config.MAX_REPAIRS`), or the task is rejected (`invalid`).

`revision` jobs are put on the revise queue by the Trace Parser after an
out-of-band verdict. New drafts arrive on the create queue (`sde:create`) from the
API; repairs and revisions on the revise queue (`sde:revise`). The Generation
Service consumes both with the same function below.

## `create` / `revise`: `generator/worker.py`

```python
from worker import run_generate_job

run_generate_job(job_id: str, job_input: dict, work_dir=None) -> dict
```

| `job_input.origin` | Other fields | What it does |
|---|---|---|
| `initial` | `spec` (taxonomy slot + difficulty profile, see `generator/prompting.py`) **or** `prompt` (raw text) | Drafts a new task. |
| `repair` | `bundle_sha256` (the failing bundle), `feedback` (the `validate` job's `feedback`) | Continues the authoring conversation with the gate evidence. Rejects a repair that drops a test function or constraint. |
| `revision` | `bundle_sha256`, `operator` (one of `prompting.OPERATORS`), `result` (e.g. `"5 of 5 runs passed."`), `evidence` (the Trace Parser's review of the five runs) | Asks for a harder or easier version (PRD §6.6). |

Returns:

```json
{
  "outcome": "OK | FAILED",
  "error": "null, or why it failed",
  "cost_usd": 0.23,
  "result": {
    "name": "taskmill-reliability-bug",
    "bundle_sha256": "…", "bundle_path": "work/bundles/<sha>",
    "draft_path": "work/drafts/<sha>.json",
    "llm_calls": [{"phase": "initial", "round": 1, "request_sha256": "…", "cached": false,
                   "prompt_tokens": 1742, "completion_tokens": 55355, "reasoning_tokens": 45356,
                   "cost_usd": 0.2337, "duration_sec": 579.5, "finish_reason": "stop",
                   "accepted": true, "errors": null}]
  }
}
```

- Up to `GENERATOR_MAX_ATTEMPTS` (3) model rounds per job: a draft that fails
  the schema checks goes back to the model with the errors. All rounds are in
  `llm_calls`.
- **Idempotent.** Every model call is cached at `work/llm/<request_sha256>.json`,
  keyed by `(job_id, round, request)`. Re-running a job after a crash reuses
  finished calls (`cached: true`, no new cost) and produces the same bundle.
  Use a new `job_id` when you want a fresh draft.
- `cost_usd` counts new spend only. It is computed from token counts, because
  the gateway's `usage.cost` for GLM-5.1 is a flat placeholder (`5e-05`);
  `gateway_cost_usd` is kept alongside.

## `validate`: `validator/gates.py`

```python
from gates import run_validate_job

run_validate_job(bundle_dir, trace_dir) -> dict   # trace_dir = work/traces/<job_id>
```

Gates, cheapest first, stopping at the first failure: `structural`, `lint`
(`harbor lint --select ALL`; blocking rules in `config.LINT_BLOCKING_RULES`),
`oracle1`, `oracle2` (reward 1 both times), `nop` (reward exactly 0 with a real
verdict).

```json
{
  "outcome": "OK | FAILED",
  "failed_gate": "null | structural | lint | oracle1 | oracle2 | nop",
  "result": {"gates": {"structural": {"passed": true, "errors": [], "warnings": []},
                       "oracle1": {"passed": true, "reward": 1.0, "exception_type": null,
                                   "tests": [{"name", "status", "message"}],
                                   "trial_dir": "…", "adopted": false}, "…": {}}},
  "feedback": "evidence for the repair job; empty when OK"
}
```

- Holds one Docker slot while it runs (three sequential Harbor runs).
- **Idempotent.** Each Harbor step runs as `--job-name <step> -o <trace_dir>`.
  A finished step is adopted on re-run (a completed job re-validates in under
  a second); a half-finished one is renamed `<step>.abandoned-<n>` and kept.
- Makes no model calls, so it costs only Docker time.

## Typical flow (also what `scripts/generate.py` does standalone, without queues)

```
create(initial)   ─► validate ─OK─► 5 rollouts (executor) ─► analyze ─► accept, or
                        │FAILED                                          revise(revision)
                        ▼
              revise(repair, feedback)   ─► validate ─► … (≤ config.MAX_REPAIRS)
```

## What every bundle guarantees

- `task.toml` is built through Harbor's `TaskConfig`: agent timeout 3600 s,
  verifier 900 s, 1 CPU / 2 GB, `agent.user = "agent"`, no `docker_image`.
- The agent and the oracle run as the unprivileged `agent` user; the verifier
  runs as root. The Dockerfile's `FROM` is pinned to a digest from
  `config/base_images.json` (`scripts/pin_base_images.py`).
- `tests/test.sh` writes a reward only for a real pytest verdict (exit 0 → 1,
  exit 1 → 0). If the verifier toolchain can't be installed, no reward is
  written and Harbor reports `RewardFileNotFoundError`, which the executor
  must classify as `INVALID`, not as a model failure.
- `README.md` in the bundle holds the hidden spec (root cause, constraints,
  pitfalls, coverage map). It is never inside the container.
