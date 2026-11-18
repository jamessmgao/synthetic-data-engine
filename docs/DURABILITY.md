# Durability and reliability guarantees

What the SDE task pipeline promises today when a worker, node, database or deploy fails, the code
that provides each promise, the test that pins it, and its limits. Only what the code on `main`
does is listed; anything not here is not guaranteed. Paths are relative to the repo root; line
numbers are as of the commit that added this file.

Defaults used below (`shared/contracts/models.py:124-130`, `shared/jobs/worker.py:64-68`,
`shared/storage/sql.py:191`):

| Setting | Default | Meaning |
|---|---|---|
| `MAX_JOB_ATTEMPTS` | 3 | claims a job may use before it is dead-lettered |
| `SDE_LEASE_TTL_S` | 600 s | lease of validate, rollout and analyze jobs |
| `SDE_AGENT_LEASE_TTL_S` | 1800 s | lease of generation jobs in agent mode (world / create / revise) |
| `SDE_HEARTBEAT_S` | 60 s | heartbeat interval (5 s while heartbeats are failing) |
| `SDE_RECONCILE_S` | 30 s | reconciler pass interval |
| `SDE_SUSPECT_MAX_WAIT_S` | 600 s | how long an expired-lease job waits for an idle process |
| `SDE_DRAIN_ON_TERM_S` | 0 (aws-prod generation: 3600) | drain window on SIGTERM |

Postgres is the source of truth. Redis only proposes job ids, and S3 holds the durable copy of
every artifact. A pod's local disk (`work` emptyDir, `infra/k8s/base/app.yaml:34`) is a cache.

## 1. Job queue and leases

| Promise | Mechanism (code) | Test | Limits / gaps |
|---|---|---|---|
| A job is leased to at most one worker at a time. | Claim is one `UPDATE … WHERE id = (SELECT … LIMIT 1) RETURNING *` that bumps `lease_token` and `attempts` (`shared/storage/sql.py:325-402`). Every store transaction is serialized: Postgres `pg_advisory_xact_lock` (`sql.py:242-245`), SQLite `BEGIN IMMEDIATE` (`sql.py:140`). | `tests/shared/jobs/test_jobqueue.py::test_concurrent_claims_never_share_a_job`, `test_claim_leases_once_in_priority_order`; `evaluation/test_executor.py::test_two_executors_run_every_job_exactly_once` | The global advisory lock serializes all claims and settles: correctness over throughput. |
| A worker whose lease was taken over changes nothing. | `heartbeat`, `settle` and `release` are compare-and-set on `(id, lease_token, status='LEASED')` (`shared/jobs/jobqueue.py:127-213`). A refused heartbeat sets `lost` and stops the work (`shared/jobs/worker.py:184-207`, `evaluation/executor.py:199-217`). | `test_jobqueue.py::test_expired_lease_is_taken_over_and_old_owner_is_fenced`; `tests/shared/jobs/test_worker.py::test_lost_lease_drops_the_result`; `test_executor.py::test_a_worker_that_lost_its_lease_records_nothing` | A fenced-out worker's side effects outside the DB (model spend, S3 uploads) still happen; only its result is dropped. |
| A job's result, the task change and the next stage's jobs commit together or not at all. | `settle(..., then=handoff)` runs the hand-off inside the settle transaction (`jobqueue.py:136-190`); follow-ups use deterministic ids and `INSERT … ON CONFLICT DO NOTHING` (`sql.py:259-316`), so a retried settle can't create them twice. | `test_jobqueue.py::test_settle_inserts_followups_once_and_inherits_pinning`, `test_insert_is_idempotent`; `tests/shared/jobs/test_services.py::test_many_pings_and_two_parsers_decide_a_version_once` | None known in the transaction. The Redis publish happens after commit (next row). |
| A job claimed by a worker that dies is re-claimed after its lease expires, at most `MAX_JOB_ATTEMPTS` times in total. | The claim query itself takes over `LEASED` rows with `lease_until < now` while `attempts < MAX_JOB_ATTEMPTS` (`sql.py:388-397`). No reconciler step is needed for this. | `tests/shared/jobs/test_crash_resume.py::test_kill_and_restart_with_two_copies`; `tests/e2e/test_e2e.py::test_kill_dash_nine_mid_run_then_restart`, `test_two_copies_one_killed_and_restarted` | Re-claim waits the full lease (10 min, or 30 min for agent jobs), plus up to `SUSPECT_MAX_WAIT_S` (next row). A crash that skips the release burns an attempt. |
| A crashing (poison) job can't drag its sibling jobs to the dead-letter. | Crash isolation: an expired-lease job is a suspect; it is claimed only by a process holding no other live lease, is marked `isolated`, and that process claims nothing else until it settles (`sql.py:348-383`). After `SUSPECT_MAX_WAIT_S` any process may take it, still isolated. | `test_crash_resume.py::test_poison_job_does_not_take_its_sibling_down`; `test_jobqueue.py::test_a_suspect_is_not_starved_by_a_busy_process` | Isolation is per process (`host:pid:` owner prefix); owners without that prefix are not isolated (`jobqueue.py:64-71`). |
| A job that keeps failing ends; it is not retried forever. | Reconciler pass: rows with `attempts >= MAX_JOB_ATTEMPTS` that are READY or have an expired lease become DEAD (`shared/jobs/reconciler.py:54-81`). A dead rollout gets its `<id>-analyze` job in the same transaction; a dead world / create / revise / validate / world_check rejects its task as `infra_unresolved`. | `test_jobqueue.py::test_reconciler_dead_letters_jobs_that_keep_killing_workers`, `test_dead_lettered_world_jobs_reject_their_tasks`, `test_dead_lettered_rollout_triggers_one_analysis`; `test_e2e.py::test_poison_job_is_dead_lettered_not_retried_forever` | Three infra failures in a row (crashes, or a job function raising: `worker.py:219-224` releases without a refund) reject the task as `infra_unresolved` even if the task itself was fine. |
| A deliberate stop does not use up an attempt. | `release(..., refund_attempt=True)` decrements `attempts` (`jobqueue.py:192-213`); used by shutdown (`worker.py:79-101`) and the Executor (`executor.py:173-178`). | `test_jobqueue.py::test_a_deliberate_release_refunds_the_attempt_a_crash_does_not`; `test_executor.py::test_shutdown_releases_every_running_job_immediately` | Only when the release itself runs (SIGTERM / SIGINT, or `main`'s `finally`). SIGKILL, OOM-kill or node loss burn an attempt. |
| A lost Redis entry never loses a job. | Jobs are added to Redis after commit and stamped `published_at` (`shared/storage/queue.py:104-115`); every reconciler pass re-adds all claimable rows (`queue.py:117-121`, `sql.py:609-618`). Redis can't grant a claim: the SQL compare-and-set decides (`jobqueue.py:75-87`). | `test_jobqueue.py::test_redis_queue_is_rebuilt_from_the_database`; `test_e2e.py::test_redis_losing_its_queues_mid_run` | A job lost from Redis waits up to one reconcile pass (30 s). |
| Half-written local artifacts don't accumulate. | Reconciler sweeps `.staging/` entries older than the lease TTL (`reconciler.py:80`, `shared/storage/artifacts.py:62-83`). | `tests/shared/storage/test_artifacts.py::test_killed_writers_leave_only_staging_entries_which_get_swept` | Only on the reconciler's pod; other pods' emptyDirs are cleaned when the pod goes. |

## 2. Worker shutdown and drain

| Promise | Mechanism (code) | Test | Limits / gaps |
|---|---|---|---|
| On SIGTERM / SIGINT a worker stops claiming and hands its running jobs back at once (READY, no backoff, attempt refunded), and stops their work. | `Shutdown.__call__` → `release_in_flight()` releases every job in `_in_flight` and calls its `on_lost` (stops the authoring trial) (`worker.py:79-101`, `128-157`); `main`'s `finally` does it again on any exit (`worker.py:558-562`). Executor: `shutdown()` cancels trials, each released with a refund (`executor.py:173-178`, `322-329`). | `tests/shared/jobs/test_drain.py::test_no_drain_hands_back_at_once`; `test_executor.py::test_shutdown_releases_every_running_job_immediately`, `test_ctrl_c_waits_for_every_worker_to_release_its_job` | The handed-back job restarts from scratch: a stopped authoring trial is "cancelled" and never adopted (section 5). Validate has the default 30 s grace, Executor 60 s (`infra/k8s/base/executor.yaml:16`). |
| On a deploy, generation pods let running authoring sessions finish (up to 60 min) before handing back what's left. | `SDE_DRAIN_ON_TERM_S=3600`, `terminationGracePeriodSeconds: 3900`, `RollingUpdate {maxSurge: 2, maxUnavailable: 0}` (`infra/k8s/overlays/aws-prod/generation.yaml:12-29`). `Shutdown` with `drain_s > 0` stops claiming, keeps heartbeats, and a deadline thread releases the rest at the deadline (`worker.py:145-171`). A second signal releases at once. | `test_drain.py::test_drain_lets_the_running_job_finish`, `test_drain_deadline_hands_the_job_back`, `test_second_signal_hands_back_at_once` | Only generation drains. Sessions still running at 60 min are restarted from scratch. Old pods drain beside the new ones, so memory and RDS connections rise for up to an hour (`generation.yaml:8-11`). |
| A worker never dies of an error in its claim loop. | Each loop catches everything, logs it, waits 5 s and goes on (`worker.py:261-277`; Executor `executor.py:279-297`). | `tests/shared/storage/test_dbretry.py::test_claim_loop_survives_an_unexpected_error` | The Trace Parser's claim loop (`evaluation/trace_parser_service.py:299-304`) has no such guard: an error in its `claim` ends that thread. The control pod's supervisor restarts the container only when a process exits (`shared/jobs/supervise.py`). |

## 3. Database and Redis outages

| Promise | Mechanism (code) | Test | Limits / gaps |
|---|---|---|---|
| A Postgres or Redis blip shorter than the lease doesn't crash a worker or lose a job. | `dbretry.retry` retries transient errors (psycopg `OperationalError` / `InterfaceError`, `PoolTimeout`, Redis connection / timeout errors, SQLite `OperationalError`) with backoff 1 s → 30 s (`shared/storage/dbretry.py:21-65`). Claim: retried until stop (`worker.py:267`, `executor.py:152`). Settle and release: retried within the lease (`worker.py:222-228`, `executor.py:176`, `189`). | `test_dbretry.py::test_retry_rides_out_transient_errors`, `test_pool_timeout_is_transient`, `test_retry_gives_up_after_its_budget`, `test_claim_loop_survives_a_blip_and_the_job_settles` | An outage longer than the lease: the settle gives up, the lease lapses, the job is re-claimed (an attempt burned) and its result dropped. Agent jobs' trials are already in S3 and are adopted (section 5). |
| A heartbeat failure never lets a lease lapse under a running job by itself. | Heartbeats use a dedicated 1-connection pool (`sql.py:195-216`, `530-540`); a heartbeat that raises is retried every 5 s, never fatal (`worker.py:184-207`, `executor.py:199-217`). | `test_jobqueue.py::test_heartbeats_go_through_while_the_main_pool_is_exhausted`; `test_worker.py::test_a_heartbeat_that_raises_is_retried_and_the_job_still_settles`; `test_executor.py::test_a_heartbeat_that_raises_keeps_the_trial_running_and_settles` | If the database stays away past the lease, the lease does lapse. |
| A worker started while the database is away waits for it. | `dbretry.retry(from_env, …, budget_s=600)` (`worker.py:538`, `executor.py:349`). | — | After 600 s the process exits and Kubernetes restarts it. |
| A failed reconciler pass is retried next pass. | `reconciler.run` catches and logs (`reconciler.py:84-92`). | — | — |
| A rolling deploy that adds a column doesn't break old pods. | No server-side prepared statements (`prepare_threshold=None`, `sql.py:205-214`). | `test_jobqueue.py::test_a_column_added_by_another_process_does_not_break_old_connections` | — |

Gaps: a Redis outage stalls all claims (the queue doesn't fall back to the database while
`REDIS_URL` is set); claims resume when Redis returns and the reconciler refills it. If the Redis
publish after a settle's commit fails, the retried settle returns False and logs "lease lost;
result dropped" although the settle did commit (`jobqueue.py:169-190`): a misleading log line
only, since the reconciler publishes the new jobs.

## 4. Node loss and Spot interruption

| Promise | Mechanism (code) | Test | Limits / gaps |
|---|---|---|---|
| On a Spot interruption notice, the node's workers hand their jobs back within seconds, attempt refunded. | AWS Node Termination Handler (IMDS mode) on every `sde/lifecycle=spot` node cordons and drains it, with a 90 s pod grace (`infra/k8s/overlays/aws-prod/spot-termination.yaml:47-79`). Generation's drain checks IMDS `spot/instance-action` at SIGTERM and every 10 s during a drain, and releases at once when a notice is there (`worker.py:104-171`). Executor and validate release at once anyway. | `test_drain.py::test_spot_interruption_at_sigterm_hands_back_at_once`, `test_spot_notice_during_a_drain_hands_back`, `test_no_spot_notice_drains_normally`, `test_imds_check_is_false_off_ec2` | Released authoring sessions restart from scratch. The IMDS check has a 1 s timeout; if it fails, generation starts a drain and re-checks every 10 s. |
| A node lost without notice loses no job. | The jobs' leases lapse and are re-claimed elsewhere (section 1); everything durable is in RDS and S3. Spot runners are `persistent` with `stop` behavior, keep their disk and rejoin by themselves (`deploy/terraform/compute.tf:89-101`); NTH uncordons on reboot. | `test_crash_resume.py`, `test_e2e.py::test_kill_dash_nine_mid_run_then_restart` (process kill, not node loss) | Each lapsed job burns an attempt and waits out its lease (up to 30 min). Local emptyDirs (work dir, dind) are lost. No test kills a real node. |
| One authoring session can't kill other jobs' containers on its node. | The authoring image's `docker` wrapper scopes every command to the trial's `sde.job=<job id>` label and refuses prunes, `-H`, `DOCKER_HOST` (`authoring/bin/docker`). | `authoring/test_docker_wrapper.py` (`test_killing_all_containers_kills_only_this_trials`, …) | The Docker socket itself is still reachable (`curl --unix-socket` bypasses the wrapper); a label-scoped socket proxy would close it. |

## 5. Authoring trials (agent mode)

| Promise | Mechanism (code) | Test | Limits / gaps |
|---|---|---|---|
| A finished authoring trial is paid for once, even if its worker dies before settling. | The trace dir is saved to S3 in a `finally` (`worker.py:376-383`, `410-417`, `444-447`) with a `.complete` marker written last (`artifacts.py:323-330`). Before running, a re-claimed job fetches it (`worker.py:411`, `443`) and `run_authoring_trial` adopts a finished trial instead of starting one (`generator/authoring_harness.py:263-271`). | `tests/shared/jobs/test_authoring.py::test_worlds_drafts_and_trials_follow_the_job_across_machines`, `test_a_trial_is_saved_even_when_the_authoring_function_raises`; `test_artifacts.py::test_trace_saved_on_one_machine_is_adopted_on_another` | A SIGKILL / node loss mid-trial skips the `finally`: nothing is saved and the trial is re-run (re-paid). |
| Cancelled or broken trials are never adopted. | `_adoptable` refuses a trial that ended in a cancel exception or lacks the job's output file (`world/world.json` or `draft.json`) (`authoring_harness.py:159-185`). Rejected trials are moved aside (`authoring.cancelled-N`, `.infra-failed-N`, `.abandoned-N`), kept for audit (`authoring_harness.py:272-283`). A half-uploaded S3 trace (no marker) is never fetched and is moved aside (`artifacts.py:332-363`). | `test_artifacts.py::test_half_uploaded_trace_is_not_adopted_and_is_moved_aside`, `test_abandoned_trace_is_kept_not_deleted` | Consequence: a session handed back at shutdown or at the drain deadline is re-run from the start. |
| A lost or cancelled job's trial is stopped, not left running and spending. | `on_lost` → `stop_authoring_trial` → `authoring_harness.stop_trial` stops Harbor's process group (30 s grace) and removes the job's labelled containers and networks (`worker.py:355-362`, `authoring_harness.py:77-103`, `357-383`). | `tests/shared/jobs/test_cancel.py::test_a_cancel_stops_the_running_authoring_trial`, `test_the_generation_service_stops_lost_authoring_trials` | — |
| The author's own stamped self-checks stand in for re-running them, only when they match exactly. | `SDE_ADOPT_AUTHOR_CHECKS=1` in aws-prod (`infra/k8s/overlays/aws-prod/kustomization.yaml:49`). Validate adopts oracle / nop / wrong-fix runs only if the stamp and bundle sha match and every role passed (`validator/gates.py:607-631`); world_check adopts only a passing check whose stamp equals this world's (`shared/taskformat/world_check.py:418-428`). | `validator/test_adopt_author_checks.py` (`test_exactly_stamped_runs_are_adopted_without_docker`, `test_anything_less_than_an_exact_passing_stamp_runs_the_gates`, `test_a_changed_world_a_missing_stamp_a_failed_check_or_off_are_not_adopted`) | Off by default in code (`shared/contracts/config.py:115-120`). |
| A bundle's leak review is paid once. | Recorded at `leak_reviews/<sha>/review.json` (`validator/gates.py:428-470`), registered to S3 by the create job (`worker.py:369-373`, `421-423`) and fetched by validate on any machine (`worker.py:478`). | `test_authoring.py::test_the_create_jobs_leak_review_is_shared_with_validate` | A record of an older `LEAK_RECORD_VERSION` is re-reviewed. |

## 6. Artifacts (S3)

| Promise | Mechanism (code) | Test | Limits / gaps |
|---|---|---|---|
| Bundles and worlds are immutable and content-addressed. | `bundle_sha256` hashes every path, mode bit and content (`artifacts.py:29-37`); a tree is written once under `<kind>/<sha>/`. | `test_artifacts.py::test_bundle_hash_covers_content_and_mode`, `test_put_bundle_is_idempotent_and_concurrent_safe` | — |
| A reader never sees a half-written artifact. | Local writes are staged and renamed (`artifacts.py:44-97`). In S3 a tree or trace counts only once its `.complete` marker exists; the marker is written after every file (`artifacts.py:282-292`, `323-341`). | `test_artifacts.py::test_killed_writers_leave_only_staging_entries_which_get_swept`, `test_half_uploaded_trace_is_not_adopted_and_is_moved_aside`, `test_publish_is_atomic_and_idempotent` | `drafts/`, `leak_reviews/` and `llm/` files are single `put_object`s with no marker (atomic per object in S3). |
| Any worker on any machine can get any artifact. | Local work dir is a cache; `bundle_path` / `world_path` / `fetch_file` / `fetch_trace` download from S3 when missing (`artifacts.py:288-341`). | `test_artifacts.py::test_second_machine_downloads_bundle_and_transcript`; `test_e2e.py::test_s3_holds_the_durable_copy` | — |
| An accepted task is published even if the publisher crashes after the ACCEPTED commit. | The Trace Parser publishes after commit and `publish_pending` re-publishes every ACCEPTED task missing from `accepted/` each pass (`evaluation/trace_parser_service.py:95-102`, `280-291`, `307-311`). | `test_services.py::test_publish_is_retried_after_a_crash` | — |

## 7. API idempotency and cancel

| Promise | Mechanism (code) | Test | Limits / gaps |
|---|---|---|---|
| Retrying a task submission with the same `task_id` creates nothing new; a different request under that id is HTTP 409. | `create_task` checks the existing row in the same transaction (`api/submit.py:192-217`); `TaskConflict` → 409 (`api/app.py:142-145`). | `api/tests/test_api.py::test_a_retried_task_id_is_idempotent_and_409_on_a_different_request`; `test_services.py::test_create_task_is_idempotent_and_conflicts_on_a_different_request` | Without a client `task_id`, the server generates one, so a retried request creates a second task. |
| A batch exists whole or not at all; retrying with the same `batch_id` is a no-op; a different request is 409. | One transaction for the batch row and all tasks (`submit.py:220-264`). | `test_api.py::test_batch_is_idempotent_and_409_on_conflict`; `test_services.py::test_create_batch_is_idempotent_and_all_or_nothing` | Same: no `batch_id`, no idempotency. |
| Resubmitting finished tasks changes nothing. | Same as above, plus deterministic job ids. | `test_e2e.py::test_resubmitting_finished_tasks_changes_nothing` | — |
| Cancel is idempotent and final: no job of a cancelled task is claimed again, and a running one settles into nothing but still records its cost. | `tasks.cancel` closes READY / LEASED jobs (`shared/jobs/tasks.py:76-93`); the claim skips decided tasks (`sql.py:396`); settle records `cost_usd` on a cancelled job (`jobqueue.py:175-183`). | `test_cancel.py::test_cancel_is_idempotent_and_leaves_decided_tasks_alone`, `test_a_worker_whose_job_is_cancelled_mid_run_records_the_cost_and_hands_off_nothing`, `test_the_queue_drops_a_cancelled_tasks_jobs` | — |

## 8. Budget

| Promise | Mechanism (code) | Test | Limits / gaps |
|---|---|---|---|
| Paid jobs stop being claimed at the pipeline cap (`SDE_BUDGET_USD`) and at a batch's `budget_usd`; concurrent claimers can't both take the last room. | The cap is part of the claim's `UPDATE` (`shared/storage/budget.py:77-93`, `sql.py:369-371`): recorded spend + an estimate for every other leased job + this job's estimate. Claims are serialized (section 1). Held-back jobs stay READY. | `tests/shared/storage/test_budget.py::test_paid_claims_stop_at_the_cap`, `test_concurrent_claimers_never_pass_the_cap`, `test_leased_jobs_count_by_their_estimate`, `test_a_batch_cap_holds_back_only_that_batch` | A job costing more than its estimate can overshoot. Unpaid jobs continue past the cap. `budget.cap_usd` reads the cap on every claim and treats unset / `none` / `off` / `unlimited` as no cap (`budget.py:53-62`). |

## 9. Deploys (`scripts/k8s_aws.py`)

No automated tests cover these; each is enforced by the deploy script.

| Promise | Mechanism (code) | Limits / gaps |
|---|---|---|
| A rejected Kubernetes object fails the deploy. | `set -o pipefail` on `kustomize … \| kubectl apply` (`scripts/k8s_aws.py:419`, `460`). | — |
| `deploy --only a,b` applies and rolls only those components. | `cmd_deploy_only` applies by label selector and restarts only those Deployments (`k8s_aws.py:383-425`); it keeps the live `SDE_AUTHORING_IMAGE` unless generation is included (`k8s_aws.py:403-410`). | — |
| Each release's images are pinned by tag, so a restart never picks up another release's code. | Images are tagged `:<release sha[:12]>` (`k8s_aws.py:120-137`, `321-341`), and `pin_tags` rewrites the overlay and fails if the edit didn't apply. Release dirs are immutable (`ship`, `k8s_aws.py:94-117`). | Unchanged templates with new image contents are restarted (`restart_unchanged`, `k8s_aws.py:374-380`). |
| A deploy never restarts the tunnels, so their public URLs stay. | `TUNNELS` are excluded from the restart selector (`k8s_aws.py:47-48`, `451-463`). | Quick-tunnel URLs change whenever the tunnel pod restarts for any other reason (node loss, eviction, a spec change), and Cloudflare gives no uptime guarantee (`infra/k8s/overlays/aws-prod/public-tunnel.yaml:1-6`). |
| A cut-off `join` resumes on re-run, and no pod lands on a node before its images are in. | New nodes register with the `sde/joining:NoSchedule` taint; a re-run treats still-tainted nodes as unfinished; every step is idempotent; the taint is removed last (`k8s_aws.py:205-212`, `228-280`). The new nodes must build the live authoring tag or the join stops (`k8s_aws.py:249-251`). | — |
| Deploys don't cut off running authoring sessions. | Generation drain (section 2). | Up to 60 min only. |

## 10. Data stores (`deploy/terraform/data.tf`)

| Store | What survives | Limits / gaps |
|---|---|---|
| RDS Postgres 16 (jobs, tasks, batches) | Pod, node and cluster restarts and deploys; automated backups kept 1 day. | Single-AZ, `skip_final_snapshot = true`, `deletion_protection = false` (`data.tf:15-37`): an AZ outage stops the pipeline, and `terraform destroy` loses the data. |
| ElastiCache Redis (job ids only) | Rebuilt from Postgres by the reconciler, so its loss loses nothing (section 1). | Single node, no replica (`data.tf:45-55`); while it is down, claims stall. |
| S3 artifacts bucket | Pod and node loss; it is the durable copy of bundles, worlds, traces, reviews and accepted tasks. Encrypted, not public. | No versioning; `force_destroy = true` (`data.tf:57-60`): `terraform destroy` deletes the contents. |
| Pod-local disk (work emptyDir, dind) | Nothing beyond the pod. | NTH deletes it on drain (`DELETE_LOCAL_DATA`). |
| SSM parameters (config, secrets) | Deploys. | `terraform apply` resets `/sde/budget_usd` to the Terraform variable (`data.tf:113-117`). |
