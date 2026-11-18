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
  --count N`. Everything is real, including generation, so its outcome depends on how good the
  generated task is.
- **Past generation:** `scripts/inject_task.py <task_dir> <task_id> --draft <draft.json>`
  hands an existing bundle straight to Build + Validate, as if the Generation Service had just
  written it. The gates, rollouts, the Trace Parser's review and its decision run for real
  without depending on generation quality. Use a bundle that passed the gates before (example
  tasks don't: they use unpinned images), with its draft record so a revision can follow.

Watch progress with `GET /v1/tasks` and read each verdict with `GET /v1/tasks/{id}`.
