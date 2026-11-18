# Testing

No test calls a real model. The end-to-end suite enforces this: every process
it starts refuses network connections that leave the machine
(`tests/e2e/netguard.py`).

| Command | What runs | Needs |
|---|---|---|
| `make test-unit` | every component's unit tests: `tests/shared` (incl. the services' hand-offs and the Trace Parser's decisions), `api`, `generator`, `evaluation` | nothing |
| `make test-e2e` | the whole pipeline as processes (below), on SQLite and, if it is up, the compose cluster | nothing; the cluster is optional |
| `make test-infra` | `tests/shared` on SQLite, Postgres, Postgres+Redis and S3 | `make deps-up` |
| `make test-all` | all of the above against the cluster | `make deps-up` |

Use `.venv/bin/python` with `shared/requirements.txt` and `generator/requirements.txt`
installed, plus `pytest` and `httpx`.

The full suite in one command: `.venv/bin/python -m pytest -q tests evaluation generator api validator authoring`.

## Component tests for agent mode, the gates and the cap

None of these calls a model or runs Harbor: model calls are scripted (`llm.complete` /
`llm._stream` replaced), Docker is a scripted `_docker`, and the authoring trial is a fake
harness.

| file | what it proves |
|---|---|
| `tests/shared/taskformat/test_leak_review.py` | the semantic leak check on **P1's real task** (`tests/shared/taskformat/fixtures/p1`: its instruction, world.json, `fix.sh` and hidden README, from task-builder `2f9d456`): P1 is flagged by the semantic review (its hook and fix sentences, verified verbatim) and by the blind prediction; the GOOD instruction passes; the reviewer sees the world and solution, the blind call only the instruction; one strong hint passes, two fail; paraphrased quotes are not verified; adoption, re-review of a v1 record, `SDE_LEAK_BLIND=0` |
| `tests/shared/taskformat/test_world_check.py` | every world_check check under `enforce` (each failure and its feedback, the dependent path); `warn` (the default) records every failure and passes, needs no new scripts; `off` skips; `sde-world check` prints a WARNING per failure and exits 0 |
| `generator/test_author_task.py` | the task author: instruction rendering (every rule: no hooks, invariant tests, seeds, heredocs, the BAD/GOOD), shapes and dials, revisions, the leak review moving into create (3 calls, adopted by validate, the world in its input), the self-check guard (with the real `ensure_task_checked`), time boxes, `instruction_size`, the mutation prompt (operators, hardness section) |
| `generator/test_author.py` | the world job: origins, lineage, the self-check guard |
| `authoring/test_sde_task.py` | `sde-task check` (schema, the wrong-fix tag, `# invariant: N` markers, fixed seeds) and `run` (roles, setup errors, one build for every role, the image kept inside a trial, the flake check) |
| `authoring/scaffold/test_scaffold.py` | the world scaffolds: replay is exact, `explore` finds a lost update, faults fire on the planned hit, repro exit codes, stdlib only (`python -I -S`), the README's C snippet compiles under `-Werror` and crashes on cue |
| `tests/shared/storage/test_budget.py` | the spend cap: claims stop at `SDE_BUDGET_USD` and resume when it rises, leased jobs count at their estimate, 16 concurrent claimers with room for 5 get exactly 5, unpaid jobs continue, the batch cap holds back only its batch, the tighter cap wins |
| `tests/shared/jobs/test_cancel.py` | a cancel during world, world_check, create, validate, rollout and analyze (nothing more is claimed or handed off, the in-flight cost is kept); a worker cancelled mid-run; reject / accept / CAS after a cancel are no-ops; the relabel script |
| `tests/shared/jobs/test_authoring.py` | the agent hand-offs: world → check → create chains, world repairs, a leaking create repaired (create FAILED / author → repair1 → validate) |
| `api/tests/test_api.py`, `api/tests/test_families.py` | the API: batches with `budget_usd`, cancel endpoints, `/healthz` and report budget blocks, `instruction_tokens`, families and the v2 sampler (a 3-family batch gets one task per family) |
| `tests/p1/test_p1_prototype.py` | `scripts/p1_prototype.py` on a fake chain: dry run by default, the budget stop, resume from the ledger |

### On Postgres, Redis and S3

`tests/shared` and `api` run on every backend in `tests/shared/conftest.py`'s matrix:
SQLite always, plus Postgres when `SDE_TEST_DATABASE_URL` is set and Postgres+Redis when
`SDE_TEST_REDIS_URL` is too (each test gets its own database and Redis key prefix). Use
`make deps-up`, or throwaway containers that can't touch a running stack:

```
docker run -d --rm --name t-pg -e POSTGRES_USER=sde -e POSTGRES_PASSWORD=sde -e POSTGRES_DB=sde_test -p 25432:5432 postgres:16.4-alpine
docker run -d --rm --name t-redis -p 26379:6379 redis:7
SDE_TEST_DATABASE_URL=postgresql://sde:sde@localhost:25432/sde_test SDE_TEST_REDIS_URL=redis://localhost:26379/15 \
    .venv/bin/python -m pytest -q tests/shared api
docker stop t-pg t-redis
```

Run queue tests under Redis: with Redis the claim only sees published jobs, so a test that
inserts jobs must `jq.publish` (or `backend.queue.publish`) them; SQLite and plain Postgres
claim from the table and hide a missing publish. Agent-mode behavior (which job types are
paid, which need Docker) depends on `SDE_AUTHOR_MODE` at import: run the budget and
authoring tests with `SDE_AUTHOR_MODE=agent` too.

## End to end (`tests/e2e`)

Each test starts the real system as separate processes and drives it the way an operator would:

```
operator ─HTTP─► API ─► store / queue / artifacts ◄──► Generation Service (create + revise)
                         (SQLite+files, or               Build + Validate
                          Postgres+Redis+S3)             Executor (+ real classifier)
                                                         Trace Parser
                                                         reconciler
```

There is no central service: each service hands off to the next stage itself, and the Trace
Parser makes the accept / revise / reject decisions (docs/PRD.md §5).

Only three things are faked (`tests/e2e/fakes.py`): GLM-5.1 drafting, the Harbor gates and
the Harbor trial. Each test scripts its tasks, for example "v1 scores 5/5, v2 scores 2/5" or
"slot 3 times out once". The fake trial writes Harbor-shaped `result.json` files, which the
executor's real classifier then scores.

**Backends.** Every test runs on SQLite + files. When Postgres, Redis and S3 are reachable on
localhost (`make deps-up`), it also runs on the cluster, each test in its own throwaway
Postgres database, reserved Redis DB and S3 bucket. It never touches the `sde` database or
the infrastructure tests' `sde_test` / Redis DB 15. Set `SDE_E2E_CLUSTER=0` to skip the
cluster, or `SDE_E2E_PG_ADMIN_URL` / `SDE_E2E_REDIS_URL` / `SDE_E2E_S3_ENDPOINT` to point elsewhere.

**What the suite checks** (`tests/e2e/test_e2e.py`):

| Test | Proves |
|---|---|
| `test_tasks_run_every_path_to_the_scripted_verdict` | Each task path ends in the right verdict: accepted at v1; revised then accepted at v2; repaired; gates failing 3 times (`invalid`); out of band at v3 (`exhausted`); invalid runs retried and never scored; a slot never valid (`infra_unresolved`); a failed draft. It also checks the exact set of jobs created, the published bundles and `verdict.md`, and the API report, cost included. |
| `test_a_batch_runs_to_its_yield_report` | The same scenario created with one `POST /v1/batches`: the batch's status, the batch list, and its report with the yield block (4 of 8 learnable). |
| `test_verdicts_explain_every_step` | Versions, revision types, the evidence handed to the model, and retried runs are all visible through the API. |
| `test_resubmitting_finished_tasks_changes_nothing` | Creating a task again with the same id is a no-op, and 409 with a different request. Rerunning finished tasks does nothing. |
| `test_kill_dash_nine_mid_run_then_restart` | Two SIGKILLs mid-run, then restarts: same verdicts, and no model call or trial repeated. |
| `test_two_copies_at_once` | Two full copies of the pipeline (on the cluster, as two machines): no job, version or publish is duplicated. |
| `test_two_copies_one_killed_and_restarted` | Both of the above at once. |
| `test_operator_drives_tasks_over_http` | `python -m api` as its own server: auth, task creation, task list, report, verdict. |
| `test_poison_job_is_dead_lettered_not_retried_forever` | A job that crashes its worker every time is dead-lettered after 3 claims. Jobs that shared its process are charged at most once and finish. |
| `test_redis_losing_its_queues_mid_run` (cluster) | With `FLUSHDB` mid-run, the reconciler rebuilds the queues from Postgres. |
| `test_s3_holds_the_durable_copy` (cluster) | Bundles, traces and the accepted task are in the bucket. |
| `test_no_model_endpoint_is_reachable_from_the_tests` | The network guard blocks off-machine hosts, including an OpenAI client. |

### Agent mode (INT, `tests/e2e/test_agent_mode.py`)

The same harness with `SDE_AUTHOR_MODE=agent`. The in-process API's
`shared.contracts.models.AUTHOR_MODE` is patched to `agent`, and the stack's environment gets
`SDE_AUTHOR_MODE=agent`. Every task then starts with a `world` job from a v2 spec. Fakes:
`fakes.world` (initial / repair / mutation; it writes `world.json` with lineage and
registers the world), `fakes.world_check` (fails the first `world_failures` rounds), and
`fakes.generate`, which writes from a world when it gets `world_sha256`, or returns
`FAILED` with `failure_kind: author` for the first `create_leak` rounds. The stack serves
one fake per job type (`tests/e2e/stack.py`).

| Test | Proves |
|---|---|
| `test_agent_mode_runs_worlds_to_the_scripted_verdicts` | Five scripted tasks reach their verdicts, and every world, draft and trial runs exactly once. |
| `test_too_easy_mutates_the_world_not_the_task` | 5/5 → a `<t>-v2-world` mutation from the parent world, with operators, rule-tagged history and `spec` (shape, dial) → world_check → create from the new world → accepted at v2. The world's lineage names its parent. No `revise` is made. The API view and the verdict say v2 is a `mutation` with its operators. |
| `test_a_world_that_never_checks_out_is_repaired_then_rejected` | 3 failing world checks → 2 world repairs, each carrying the last world and the feedback → REJECTED invalid, with no create. A world repaired once is used for the task. |
| `test_a_leaking_draft_is_repaired_from_its_world_not_rejected` | create FAILED (`failure_kind: author`, the leak review) → `repair1`, carrying the world, the leaked bundle and the feedback → validate → accepted. |
| `test_too_hard_revises_the_task_on_the_same_world` | 0/5 → `revise` origin `easier` with the same `world_sha256`; no new world. |
| `test_the_real_agent_adapters_across_two_machines` (cluster only) | With `SDE_E2E_REAL_ADAPTERS=1`, `shared/jobs/worker.py`'s real `world_fn`, `draft_fn` and `world_check_fn` run as production does: fetching worlds, bundles and drafts from S3, registering what was written, saving authoring trials, mapping results. Only `author.run_world_job` / `run_create_job` and `world_check._docker` are replaced (`fakes.author_*`, `fakes.docker`); the real structure check, Dockerfile rules and check-image flow run. Two machines share S3, so the stand-ins' assertions that everything is local prove the cross-machine fetches. |

**"Never paid twice."** The fakes log `start`, `finish` and `adopt` per job id. The suite
asserts that no model call or trial finishes twice. The one exception is the case PRD N2
allows: a trial that a killed process had finished but not settled, reclaimed on a *different
machine*. On one machine the restart adopts the finished work, and the suite requires zero
repeats.

**Adding a case.** Add a task to a scenario dict (see the marks in `fakes.py`), run it, then
assert on the API or the store. `expected_job_ids()` derives the exact job set for the
standard paths.

## Live runs on a deployed stack

The e2e suite fakes GLM-5.1, the gates and Harbor; a live run uses the real ones. Deploy the
compose stack as its own project (it must not join another session's `deploy` project):

```
SDE_API_KEY=... docker compose --env-file .env -p sde-live -f deploy/compose.dev.yaml \
    --profile app up -d --build        # add SDE_*_PORT overrides if the defaults are taken
```

Two kinds of live test:

- **The whole loop:** `POST /v1/tasks` (`{"prompt": ...}` or `{}`), or `scripts/run_tasks.py
  --count N --batch <id>` (one `POST /v1/batches`, then its report and yield). Everything is real, including generation, so its outcome depends on how good the
  generated task is.
- **Past generation:** `scripts/inject_task.py <task_dir> <task_id> --draft <draft.json>`
  hands an existing bundle straight to Build + Validate, as if the Generation Service had just
  written it. The gates, rollouts, the Trace Parser's review and its decision run for real
  without depending on generation quality. Use a bundle that passed the gates before (example
  tasks don't: they use unpinned images), with its draft record so a revision can follow.

Watch progress with `GET /v1/tasks` and read each verdict with `GET /v1/tasks/{id}`.

What the live runs found is in docs/ANALYSIS.md §11–12. In particular, an injected pilot
task whose race tests detect the bug only some of the time passed nop once and failed it
the next time; the gates caught the flake.

## Unit suites added with agent mode

- `tests/shared/jobs/test_authoring.py`: the agent adapters, per-job dispatch (a job with
  `world_sha256` runs the agent author, anything else one-shot), the world carried
  through validate into repairs, a raising authoring function still saving its trial,
  and the leak review shared between create and validate.
- `tests/shared/jobs/test_cancel.py`: cancel at every stage. The running job's settle is
  fenced but its cost is kept; nothing is handed off.
- `tests/shared/jobs/test_worker.py` and `evaluation/test_executor.py`: a heartbeat that
  raises is retried and the job still settles.
- `tests/deploy/test_compose_env.py`: every setting the code reads is listed in
  `deploy/local.env.example`, and compose doesn't shadow it.
- `tests/shared/taskformat/test_world_check.py`, `tests/shared/taskformat/test_leak_review.py`,
  `generator/test_author*.py`: the gates and the authoring kit (task-builder).

The full local run: `SDE_TEST_DATABASE_URL=… SDE_TEST_REDIS_URL=… SDE_TEST_S3_ENDPOINT=…
.venv/bin/python -m pytest -q tests evaluation generator api experiments validator`
(656 passed, 3 skipped on main `d0130b4`).
