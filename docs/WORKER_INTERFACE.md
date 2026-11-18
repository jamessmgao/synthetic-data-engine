# Worker interface: every job type, as built

Each stage of the pipeline is a job type with one job function. The function holds
no database state: a service loop leases the job row, calls the function, and settles
what it returns together with the hand-off to the next stage, in one transaction
(`shared/jobs/jobqueue.py:JobQueue.settle`, hand-offs in `shared/jobs/handoffs.py`,
docs/PRD.md §5). Paths are under the work dir, `config.WORK_DIR` (`SDE_WORK_DIR`,
default `./work`); in production the artifact store mirrors it to S3.

`SDE_AUTHOR_MODE` (`shared/contracts/models.py`) picks how tasks are authored:

- `oneshot` (the default): `create` / `revise` are one GLM-5.1 call per draft
  (`generator/worker.py:run_generate_job`);
- `agent`: each version starts from a **world** (docs/WORLD_CONSTRUCTION.md):
  `world` → `world_check` → `create` → `validate` → rollouts. `world`, `create` and
  `revise` are each a Harbor authoring trial, terminus-2 + GLM-5.1 in the authoring
  image (`generator/authoring_harness.py:run_authoring_trial`), holding a Docker slot.

## The flow

```
agent mode only:
  world / world-repair<k> ─OK─► world-check-r<k> ─OK─► create (from the world)
        │FAILED                      │FAILED (base checks, or hardness under enforce)
        ▼                            ▼
   REJECTED invalid          world-repair<k+1> (k < MAX_REPAIRS_PER_VERSION = 2), else REJECTED invalid

both modes:
  create / revise / repair<k> ─OK─► validate-r<k> ─OK─► 5 rollouts ─► analyze (per settled run)
        │FAILED                        │FAILED                         │
        ▼                              ▼                               ▼ all 5 slots scored
   repair<k+1> if failure_kind is   repair<k+1> (≤ 2), else       1-3 passes: ACCEPTED
   "author" and a bundle was        REJECTED invalid              out of band: version n+1
   written (≤ 2), else REJECTED                                   (agent: too easy → world mutation,
   invalid                                                         too hard → revise "easier",
                                                                   task blamed → revise "repair_ambiguity")
                                                                  out of band at MAX_VERSIONS: REJECTED exhausted
```

Every task change is a compare-and-set on `(current_version, stage)` (`shared/jobs/tasks.py:cas`):
if the task moved on (decided, cancelled, or another copy got there first), nothing is
enqueued. See docs/VALIDATION_GATES.md for what each gate checks.

## Job ids

Deterministic, so a retried settle can't create a job twice
(`shared/storage/sql.py:job_id`, `rollout_job_id`):

| job | id | type |
|---|---|---|
| a version's world (initial or mutation) | `<task>-v<n>-world` | `world` |
| world repair k | `<task>-v<n>-world-repair<k>` | `world` |
| the check of world draft k | `<task>-v<n>-world-check-r<k>` | `world_check` |
| v1's task | `<task>-v1-create` | `create` |
| a later version's task (a revision) | `<task>-v<n>-revise` | `revise` |
| repair k of a version's task | `<task>-v<n>-repair<k>` | `revise` |
| the gates on draft k | `<task>-v<n>-validate-r<k>` | `validate` |
| run `slot`, attempt `retry` | `<task>-v<n>-rollout<slot>-r<retry>` | `rollout` |
| the Trace Parser ping for a run | `<rollout id>-analyze` | `analyze` |

`shared/jobs/handoffs.py:draft_round` and `world_round` read k back from the id. Queue
priority (`shared/contracts/models.py:PRIORITY`): analyze 40, rollout 30, validate 20,
world_check 18, revise 15, create 12, world 10: finish what's started first.

## `world`: `generator/author.py:run_world_job` (agent mode)

```python
run_world_job(job_id, job_input, work_dir=None, *, run_trial=None) -> dict
```

| `job_input.origin` | other fields | what it does |
|---|---|---|
| `initial` | `spec` (taxonomy v2: family, mechanism, shape, environment, verification, dial, seed?) | a new world (`generator/prompts/world.md`, with the family brief from `generator/families.py:brief_for`) |
| `repair` | `world_sha256`, `feedback` (the world_check's), `spec` | fix a world that failed world_check; lineage kept |
| `mutation` | `parent_world_sha256`, `spec`, `mutation` {operators, why}, `mutations` (history), `analysis`, `target_fix`, `result`, `evidence` (`evaluation/trace_parser_service.py:next_job`) | make the strategy that passed fail (`generator/prompts/world_mutation.md`, rendered by `generator/author_task.py:render_world_mutation_instruction`). Its `hardness_section` renders an optional `hardness` key, which nothing sets today (parked, docs/TODO.md), so it reads "(not recorded for the parent world)" |

Returns `{outcome, error, cost_usd, result}`; `result` on OK:
`{world_sha256, world_path, name, authoring}`. The world is stored content-addressed at
`<work>/worlds/<world_sha256>/` with world.json's `spec` and `lineage` set by the
pipeline (`lineage = {parent_world_sha256, mutations}`; the author's
`lineage.mutations[-1].defeats` is kept). `authoring.self_check = {by, outcome}`.

Before returning OK it checks the structure (`shared/taskformat/world_check.py:_check_structure`),
size limits (`MAX_WORLD_FILES`, `MAX_WORLD_BYTES`), and that the author self-checked
(`generator/self_check.py:ensure_world_checked`: the saved `/logs/artifacts/world_check.json`
is used, or the check is run once here; a failing one is FAILED / author).

The time box is `AUTHOR_TIMEOUT_SEC` = `SDE_WORLD_TIMEOUT_S` (default 1800 s).

## `world_check`: `shared/taskformat/world_check.py:run_world_check_job` (agent mode)

```python
run_world_check_job(world_dir, trace_dir) -> {outcome, failed_check, result: {checks, hardness?}, feedback, cost_usd: 0}
```

Deterministic, no model call, holds a Docker slot. The base checks (structure, build,
repro fails twice, repro passes twice after the fix) gate the world; the hardness
checks run unless `SDE_WORLD_HARDNESS=off` and are recorded in `result.hardness`:

```json
{"mode": "warn", "symptom_ok": true, "spans_or_dependent": false, "wrong_fix_ok": true,
 "failed": ["fix_not_local"],
 "details": {"fix_files": ["/app/worker.py"], "via": null, "missing": ["..."]}}
```

Under `warn` (the default) they never fail the world; under `enforce` the first failing
one does. Every check that ran is in `result.checks[name] = {passed, exit_codes, output}`.
The record is written to `<trace_dir>/world_check.json` and adopted on retry.
Details: docs/VALIDATION_GATES.md.

## `create` / `revise`

### One-shot: `generator/worker.py:run_generate_job`

| `job_input.origin` | other fields | what it does |
|---|---|---|
| `initial` | `spec` (v1 or v2) **or** `prompt` (raw text) | drafts a new task |
| `repair` | `bundle_sha256`, `feedback` (the gate's) | continues the authoring conversation with the gate evidence; a repair that drops a test or constraint is refused |
| `revision` | `bundle_sha256`, `operator`, `direction`, `result`, `evidence`, `mutation_operators`, `target_fix`, `mutations` | a harder or easier version (PRD §6.6) |

Up to `GENERATOR_MAX_ATTEMPTS` (3) model rounds; every call is cached at
`work/llm/<request_sha256>.json`, so a re-run reuses finished calls (`cached: true`, no
new cost). `result.llm_calls` lists every round.

### Agent mode: `generator/author_task.py:run_create_job`

```python
run_create_job(job_id, job_input, work_dir=None, *, run_trial=None, leak_review=None, self_check=None) -> dict
```

| `job_input.origin` | other fields |
|---|---|
| `initial` | `world_sha256`, `shape`, `dial` |
| `repair` | `bundle_sha256`, `feedback`, `world_sha256`? (the gates, the leak review or the self-check failed) |
| `easier` | `bundle_sha256`, `world_sha256`, `result`, `evidence`, `target_fix`, `mutations` (too hard; written by `trace_parser_service.next_job`; `analysis` is read if present but not written) |
| `repair_ambiguity` | the same fields (a failure blamed on the task) |

(A one-shot style `{"origin": "revision", "direction": "easier" | "repair"}` maps to
`easier` / `repair_ambiguity`. Without `world_sha256` a revision uses the world its parent
draft was built from.) The author reads the world at `/input/world/`, a revision's
previous draft at `/input/previous/draft.json`, and writes `/logs/artifacts/draft.json`
(docs/TASK_GENERATION_RULES.md). The time box is `SDE_CREATE_TIMEOUT_S` (default 1200 s).

After the trial, in order:

1. the draft is validated as a `TaskDraft` (`shared/taskformat/schema.py`), needs a
   `wrong_fix_script`, and a revision may not drop tests or constraints (`_repair_guard`);
2. the bundle is written content-addressed (`shared/taskformat/materialize.py:write_bundle`;
   `task.toml`'s `[metadata.generation]` records `world_sha256`, origin, shape, dial,
   `prompt_sha256`);
3. **the self-check guard** (`generator/self_check.py:ensure_task_checked`): the author's
   saved `sde-task check` result, or the check run here (deterministic, free). Failing →
   FAILED / author with the errors, the bundle kept;
4. **the leak review** (`validator/gates.py:gate_leak`, the same gate validate runs, without
   a Docker slot): recorded by bundle sha at `<work>/leak_reviews/<sha>/review.json`, so
   validate adopts it at no cost. A leak → FAILED / author with the reviewer's feedback and
   the bundle. If the review call itself errors, the job still succeeds and validate
   reviews it.

### Result (both create paths)

```json
{
  "outcome": "OK | FAILED",
  "error": "null, or why",
  "cost_usd": 2.39,
  "result": {
    "name": "durable-reservation-retry", "bundle_sha256": "…", "bundle_path": "work/bundles/<sha>",
    "draft_path": "work/drafts/<sha>.json", "world_sha256": "…", "llm_calls": [],
    "authoring": {"trial_dir": "…", "steps": 31, "duration_sec": 1296.0,
                  "n_input_tokens": 812000, "n_output_tokens": 30500, "gateway_cost_usd": 5e-05,
                  "adopted": false, "exception_type": null,
                  "instruction_size": {"chars": 17294, "tokens_est": 4324}},
    "self_check": {"by": "author | pipeline", "runs": {"oracle": true, "nop": true, "wrong": true}},
    "leak": {"passed": true, "direct": 0, "strong_hints": 0, "blind_leak": false, "calls": ["…"],
             "cost_usd": 0.06, "adopted": false, "had_world": true},
    "failure_kind": "infra | author   (FAILED only)",
    "feedback": "what to fix   (FAILED author with a bundle only)"
  }
}
```

- **`cost_usd` is computed from token counts** (`generator/authoring_harness.py:summarize`,
  `shared/llm/llm.py`), because the gateway's `usage.cost` for GLM-5.1 is a flat placeholder;
  it is kept as `gateway_cost_usd`. The leak review's cost is added.
- **`instruction_size`** is the rendered instruction's size (`authoring_harness.py:instruction_size`,
  `tokens_est` = chars / 4, within ~10% of cl100k on our prompts), the context every agent
  step starts from; the report aggregates it as `instruction_tokens`.
- **`failure_kind`**: `infra` when the trial failed without leaving a draft (a Harbor
  exception, no trial); `author` when a clean trial left no valid draft, or a draft the
  self-check or the leak review rejected. A valid draft is accepted even if the trial
  ended with an exception after writing it (`authoring.exception_type` records it).
- **Repair routing** (`shared/jobs/handoffs.py:after_generation`): a FAILED create / revise with
  `failure_kind == "author"`, a `bundle_sha256` and `draft_round(job) < MAX_REPAIRS_PER_VERSION`
  enqueues `repair<k+1>` (type `revise`, input `{origin: "repair", bundle_sha256, feedback,
  world_sha256}`), the same shape as a gate repair. Anything else is REJECTED `invalid`.
- **Idempotent per job id**: the harness adopts a finished trial under `traces/<job_id>/`,
  bundles are content-addressed, and the draft record and leak review are written once.

## `validate`: `validator/gates.py:run_validate_job`

```python
run_validate_job(bundle_dir, trace_dir) -> dict   # trace_dir = work/traces/<job_id>
```

```json
{"outcome": "OK | FAILED",
 "failed_gate": "null | structural | lint | oracle1 | oracle2 | nop | wrong_fix | leak",
 "result": {"gates": {"structural": {"passed": true, "errors": [], "warnings": []},
                      "oracle1": {"passed": true, "reward": 1.0, "tests": [{"name", "status", "message"}], "…": "…"},
                      "leak": {"passed": true, "…": "…"}}},
 "feedback": "evidence for the repair; empty when OK",
 "cost_usd": 0.0}
```

Gates run cheapest first and stop at the first failure (docs/VALIDATION_GATES.md). Each
Harbor step runs as `--job-name <step> -o <trace_dir>`: a finished step is adopted on
re-run, a half-finished one renamed `<step>.abandoned-<n>`. In agent mode the leak gate
adopts the create job's review, so validate makes no model call.

## `rollout`: `evaluation/executor.py`

Input: `{task_dir}`; the row carries `slot`, `retry`, `bundle_sha256`, `config_hash`. One
Harbor trial of the pinned agent (terminus-2 + `openrouter/z-ai/glm-5.3-flash`,
`reasoning_effort=high`, `evaluation/harbor.py:pinned_agent`), classified by
`evaluation/classify.py:classify`:

- `VALID_PASS` / `VALID_FAIL`: a real verdict (reward 1 / 0);
- `INVALID`: an exception, no verifier result, a non-binary reward, or an agent config that
  doesn't match the pinned one. Never scored: the same slot is retried (`-r<retry+1>`) with
  backoff, up to `MAX_ROLLOUT_RETRIES` (3).

The settle records `outcome, reward, cost_usd, trace_uri, error` and enqueues the
`analyze` ping. The trace is uploaded before the settle, so another machine adopts a
finished trial instead of rerunning it.

## `analyze`: `evaluation/trace_parser_service.py`

Settles with `{outcome: "OK", result: {action, summary, …}}`, where `action` is `stale`
(the task moved on), `wait` (slots still open), or the decision once all five slots are
scored: accept, a revision (with the operators chosen by
`evaluation/calibration.py:choose_mutations` and the rule + evidence for each), or a
reject. In the same transaction it writes `trace_metrics` and `failure` (the GLM-5.1
review of each failed run: `explanation`, `attribution` model | task | unexplained,
`failing_tests`, `evidence`, `cited_evidence`, `pitfall_matched`) onto the run rows.

## Cancel and budget

- A cancelled task (`shared/jobs/tasks.py:cancel`, docs/API.md) has its READY / LEASED jobs
  closed (DEAD, `cancelled: …`); a job still running settles into nothing, but its
  `cost_usd` is still recorded (`JobQueue.settle`).
- Paid job types are claimed only under the spend cap (`shared/storage/budget.py`, docs/API.md).

## What every bundle guarantees

- `task.toml` is built through Harbor's `TaskConfig`: 1 CPU / 2 GB,
  `agent.user = "agent"`, no `docker_image`; `[metadata.generation]` records how it was made.
- The agent and the oracle run as the unprivileged `agent` user; the verifier as root. The
  Dockerfile's `FROM` is pinned to a digest from `config/base_images.json`.
- `tests/test.sh` writes a reward only for a real pytest verdict (exit 0 → 1, exit 1 → 0);
  otherwise Harbor reports `RewardFileNotFoundError`, which the executor classifies `INVALID`.
- `README.md` in the bundle holds the hidden spec (root cause, constraints, pitfalls,
  coverage map). It is never inside the container. `solution/wrong_fix.sh` is the plausible
  wrong fix (agent mode always has one).
