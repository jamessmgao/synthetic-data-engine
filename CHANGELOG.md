# Changelog

Big changes only, newest first. Each heading gives the hour the change started, in PST, from its
first commit. (docs/ITERATION_LOG.md uses UTC, 8 hours ahead.) The history was squashed on
2026-11-24: commit ids cited here and in docs/ are labels from the development history, not
commits in this repository.

## 2026-11-25, 1 PM PST: new title and deck, the accepted tasks, no archive

**What changed**
- **Title.** The README is titled "Agentic RL Environment Generation Platform". The repository
  name and the `sde` prefix are unchanged.
- **Deck.** docs/slides/ holds the new presentation
  (`agentic-rl-environment-generation-platform.pptx`) and its 9-slide image, which the README
  embeds. They replace the earlier deck.
- **Accepted tasks.** `accepted_tasks.zip` at the repo root holds the 46 accepted tasks, each
  with its `verdict.md`: the versions and world mutations, every agent run, and why the failed
  runs failed.
- **docs/archive/ is deleted.** The early analyses, the plans, the first design spec, the goals checklist and the demo list are gone from the tree. References to them in docs and
  comments were removed.

**Why**
- The archive held superseded plans and one-off material. The current docs describe the
  system as built.

**Tradeoffs**
- Older entries below still name files that were in docs/archive/; none of them exists now.
- The bundles in `accepted_tasks.zip` use the repo's current names (task org, author, `sde_*`
  scaffold modules), so they differ from the accepted bytes by those renames. They were not
  re-run.

## 2026-11-24, 12 PM PST: layout tidy: leftovers deleted, deploy tooling in one place, real packages

**What changed**
- **Deleted (e8d8cdc).** Files nothing used: `examples/`
  (six hand-written example tasks), `harbor/` (vendored Harbor files; Harbor is installed as a package),
  the committed `output/` (old batches, best-task copies, the taskmill and
  f1 task directories, rendered P1 prompts) and three unused scripts (`scripts/usage.py`,
  `scripts/generate.sh`, `scripts/evaluate.sh`). `output/` is now a gitignored runtime
  directory.
- **Moved (e8d8cdc).** The P1 fixture `output/p1/p1-local-1` is `tests/fixtures/p1-local-1`
  (the load test and the chaos test inject it). The P1 prototype, its prompt renderer and its
  test moved from `scripts/` and `tests/p1/` to `experiments/`.
- **Docs archived (e8d8cdc).** `K3S_PLAN.md`, `K8S_PLAN.md`, `SPEC.md`,
  `DEMO_EXAMPLES.md`, `AGENT_MODE_DEPLOY.md` and `slides/` moved to docs/archive/ (deleted the
  next day; the slides are back in docs/slides/). The two
  "Moved" pointers (`HILLCLIMBING_ALGORITHM.md`, `ITERATION_RUNBOOK.md`) are gone.
  docs/KUBERNETES.md is an index: docs/DEPLOY.md §8 for the cluster as built, the archived
  plans for the design.
- **Deploy tooling in one place (f578e03).** `infra/k8s/` is `deploy/k8s/`, and
  `scripts/k8s_aws.py` and `scripts/k8s-local.sh` are in `deploy/`. Pure moves: all four
  overlays render byte-identical to before. `aws_ops deploy` and `k8s_aws` now share one
  release upload (`aws_ops.upload_release`).
- **Real packages (0d75e7e).** `generator`, `validator` and `authoring` are packages, imported
  by qualified name (`from generator import config`). The `sys.path` inserts that reached into
  those directories are gone. A script run by path (`sde-task`, `sde-world`, `generate.py`,
  `materialize.py`, `gates.py`) swaps its own directory for the root on the import path, so
  the authoring image needs no `PYTHONPATH`. `authoring/scaffold` stays flat, because it is
  copied into generated worlds.
- **Service boundaries.** The service packages (`api`, `generator`, `validator`, `evaluation`,
  `authoring`) no longer import each other. What they share is under `shared/` (`storage`,
  `jobs`, `contracts`, `taxonomy`, `taskformat`, `llm`), and `pipeline/` is gone. The two worker
  entry points are `python -m generator.service` and `python -m validator.service`, with the
  arguments `python -m pipeline.worker` took. No behaviour change. `CLAUDE.md` states the rule
  and `tests/test_boundaries.py` enforces it.
- **e2e tests fixed (33391b6).** `tests/e2e` had failed 6 to 8 of 15 tests since early
  stopping landed (af4cdb8): it decides a version on whichever agent runs finish first, and the
  scripted scenarios assert exact jobs and pass counts. The test stack now pins early stopping
  off, and one new test turns it on. The cluster-only tests skip when the data services are
  down.
- **`infra_unresolved`, as documented.** A slot that never gets a valid agent run rejects the
  task only when the scored runs don't already make the verdict certain. With 1 pass and 3
  fails the version is accepted on the 4 scored runs. The code already did this; the docs now
  say so (docs/VALIDATION.md, docs/EVALUATION.md, docs/PRD.md §6.4).
- **Structural validator rewritten.** `validator/validate.py` now checks a task directory with
  Harbor's own models (`TaskConfig`, `TaskPaths`, `Task`) plus the pipeline's rules, and has its
  own tests (`validator/test_validate.py`). Harbor is a hard import there, as it already was in
  `generator/materialize.py`. `validator/requirements.txt` is gone: nothing used it.
- **Test commands.** `make test-unit` and `make test-all` run every suite: `tests/pipeline`,
  `tests/deploy`, `api`, `generator`, `evaluation`, `validator`, `authoring` and `experiments`
  (they ran four of the eight before).
- **docs/DEPLOY.md.** It now says that production runs on K3s (§8.6) and that §2 and §4 to §6
  describe the Compose deployment; sizes follow Terraform's defaults and the aws-prod overlay.
- **Gateway base URL out of the repo.** `OPENROUTER_API_BASE` has no default in the code, the
  compose file or the deploy scripts. Locally it comes from `.env` (`.env.example` shows the
  shape). On AWS, `make aws-secrets` copies it from `.env` to SSM `/sde/openrouter_api_base`, and
  both deploys read it from there. A model call without it fails with a message that names the
  variable.

- **One name prefix, `sde`.** Every project-prefixed name now uses `sde`: the `SDE_*` settings, the `sde-task`
  and `sde-world` CLIs, the scaffold modules, the image names, the namespace, `/opt/sde`, the
  `/sde/*` SSM parameters, the `sde:` Redis prefix, the Terraform names and the skill
  (`.claude/skills/sde-pipeline-api`). The change is in the repo only.

**Why**
- The repo still had unused files and committed run output, deploy tooling in three
  directories, and imports that only worked through path edits. A reader could not tell what was
  live.

**Tradeoffs**
- The next AWS deploy needs `make aws-secrets` run once first, with `OPENROUTER_API_BASE` in
  `.env`. Without the SSM parameter the deploy stops with a message that says so.
- A release from before f578e03 still has `infra/k8s`, so `make aws-k8s-deploy REF=<older sha>`
  fails with the current script. Rolling back to one needs `scripts/k8s_aws.py` from that
  checkout.
- The scripted e2e scenarios no longer exercise early stopping; the one new test and the unit
  tests in tests/pipeline/test_services.py do.
- The deployed AWS stack still uses the earlier prefix. Nothing was run against AWS. This tree's
  Terraform must not be applied to the existing state (it would destroy and recreate the
  database and the buckets), and its `make aws-*` targets do not work against the deployed
  cluster. deploy/terraform/README.md lists the differences.
- Local state under the earlier prefix is orphaned: the SQLite file in `work/`, the compose
  Postgres volume, a k3d cluster, and settings in `deploy/local.env` or `.env`. Rename the
  settings to `SDE_*` and recreate the rest.

## 2026-11-18, 5 PM PST: the final result, the README's "How we got here" and the presentation

**What changed**
- **Result.** 0 of the first 16 tasks landed in the band (iteration 0: 0 of 10; iteration 1:
  0 of 6). At the end about 60% of decided tasks did: 46 accepted of 77 (README "How we got
  here").
- **README "How we got here" (daa153d to 6545108).** It names the method: a taxonomy that seeds
  bug generation, mixed with world generation, and a double feedback loop. Loop 1 is the Trace
  Parser's revisions inside the pipeline. Loop 2 is the agentic coding loop around it, run with
  `/goal`. It lists the loop's four highest-leverage optimizations: prompt engineering, taxonomy
  seeding, trace-driven mutations and generation control.
- **Slides (5ebbb16, a3fdfac, 134ecf3).** docs/slides/ holds the deck
  (`synthetic-data-engine-presentation-v7.pptx`) and the 9-slide overview image
  (`synthetic-data-engine-slides.png`), which the README embeds. (Replaced on 2026-11-25 by
  `agentic-rl-environment-generation-platform.pptx` and its `-slides.png`.)
- **Demo examples (e71fa8d).** docs/DEMO_EXAMPLES.md (deleted on 2026-11-25) lists example tasks with links to each
  agent run in the dashboard.

## 2026-11-18, 5 PM PST: docs cleanup, the API reference and a Claude skill

**What changed**
- **Project rename (3aefff5, f5cd88e).** The project is `synthetic-data-engine`, with a new
  README. The original spec is
  docs/SPEC.md (deleted on 2026-11-25).
- **Merged docs (f5cd88e, 925adf4, d58c922, 4c37f01).**
  - docs/HILLCLIMBING.md merges the algorithm, the goal and the runbook.
    docs/HILLCLIMBING_ALGORITHM.md and docs/ITERATION_RUNBOOK.md are now pointers to it
    (both deleted on 2026-11-24).
  - docs/DIFFICULTY.md merges three difficulty docs.
  - docs/VALIDATION.md replaces the three validation docs; the unique failure write-ups moved
    into docs/AGENT_FAILURE_ANALYSIS.md.
  - docs/AUTHORING.md (was `AGENTIC_AUTHORING_PLAN.md`) is rewritten as built.
- **Archive (925adf4).** `ANALYSIS.md`, `PILOT_RESULTS.md` and `LINKS.md` moved to
  docs/archive/.
- **New docs.** docs/DURABILITY.md, docs/CAPACITY.md,
  docs/AGENT_FAILURE_ANALYSIS.md, docs/TASK_CREATION_OPTIMIZATION.md, and round 2 of
  docs/LOAD_TEST.md (the production cluster under real load).
- **Index and links (a72a6cb, e5a254b).** The README has a grouped docs index. A repo-wide link
  rewrite left 0 broken links.
- **API reference and skill (08145f0, 9098c67).** docs/API.md §7 "Every route" lists every
  route and field. The README's API section covers only submitting and following jobs.
  `.claude/skills/sde-pipeline-api/SKILL.md` is a Claude skill that drives the API: health and
  spend, submitting and cancelling batches, queues, tasks and failure explanations.

**Why**
- The docs had grown one file per question. Several overlapped, and some still described plans
  that the build had since changed.

**Tradeoffs**
- docs/KUBERNETES.md is only an index. The as-built merge of docs/K3S_PLAN.md and
  docs/K8S_PLAN.md (both deleted on 2026-11-25) is still to do.

## 2026-11-18, 3 PM PST: Trace Parser revision loop: early stopping, timeout review, review-led mutations

**What changed**
- **Early stopping (af4cdb8, 63a7ea5).** A version is decided once its verdict is certain: with
  p passes and r slots left, p ≥ 1 and p + r ≤ 3 is in band, and p ≥ 4 is too easy. It never
  stops on too few passes. The other agent runs close as `SKIPPED`, and the API still reports
  the version's pass count.
- **Timeout review (856db8c, f1c822e).** An agent run that hits its 1-hour limit is `TIMEOUT`
  and waits for the Trace Parser's ruling. A model failure (a wedged terminal, a loop, its own
  code hung) becomes `VALID_FAIL`. Anything else becomes `INVALID` and is retried. A timeout
  is not a failure by itself.
- **Mutation choice (ba4c0ec, 5b7290d, 9af8ab7).** The pass review's suggestions outrank the
  keyword rules. `remove_cues` doesn't count toward `MAX_MUTATIONS`. `add_persistent_state` is a
  last resort (0 of 7 such mutations reached the band).
- **What the mutation author sees (363d2d0, 6f13462).** How the passing agent runs found the
  bug (`/input/solver_runs/run<k>.md`), and the parent world's hardness results.
- The three revision paths are unchanged: too easy → a world mutation, too hard → `easier`,
  task blamed → `repair_ambiguity` (docs/EVALUATION.md).

**Why**
- Agent runs are the main cost of a version, and a certain verdict doesn't need the rest of
  them. A timeout can be the model's fault or the task's, so the trace has to decide.

**Tradeoffs**
- An early-stopped version has fewer than five scored agent runs (for example 2/4).
- The timeout review is one more model call per timed-out agent run.

## 2026-11-18, 1 PM PST: taxonomy expansion, world reuse and variants

**What changed**
- **Taxonomy (10fbaf1, 8db82d6).** `pipeline/taxonomy.py` now has six families with 62
  mechanisms (22 before, per the README) and a new application-domain dimension: 40 application
  domains, plus 10 language systems for the compilers family. Each new spec gets its family's
  least-used domain. Every new mechanism carries a one-line defect pattern
  (`MECHANISM_NOTES`), and the family briefs in `generator/families.py` were rewritten to match.
- **Retired mechanisms.** `greedy_counterexample` and `optimality_required` (greedy versus
  optimal names its own remedy: 0 accepted of 11), `scalability_wall` and `memory_bound`.
- **World reuse (9103a3e, f94889f, 725a329).** `reuse_worlds: true` on a batch authors tasks on
  existing checked worlds and builds no new ones. `pipeline/world_selection.py` picks the world
  from each world's history; `GET /v1/worlds` lists the candidates.
- **Variants (a32b825, 8abab96, 2848998).** A reuse is a reseeded variant of the picked world:
  the old defect is fixed and one defect of a new mechanism is planted
  (`generator/prompts/world_variant.md`). The task author sees the world's earlier tasks and
  writes a different one.
- Choosing the idea, building the world and writing the task stay separate jobs, so one world
  can carry several tasks and one mechanism can land in several worlds (docs/AUTHORING.md).

**Why**
- Ideas clustered: at one point the ledger domain had 13 of 46 calibrating tasks
  (docs/ITERATION_LOG.md). Domains spread the same bug pattern across different systems.
- A checked world is the expensive part of a task, so reusing one is cheaper than building one.

**Tradeoffs**
- A plain second task on the same world repeated the first, which is why a reuse became a
  variant. Worlds with greedy defects or blind-only leaks are never reused.

## 2026-11-18, 9 AM PST: dashboard (web/)

**What changed**
- **A read-only UI over the API (fd0bca1).** Next.js, in `web/`: the status and results of every
  batch, task and job, down to bundles, worlds and agent run traces. On `aws-prod` it also runs
  as a pod with its own public link (eebe8d4). See web/README.md.
- **Running board (6f034b7).** "Running now" is a kanban by stage, one card per task, with a
  Board / Table toggle.
- **Analytics page (225864a, 2026-11-19).** Yield by family and mechanism, versions to accept,
  paths to the band and revision moves.
- **Prod / Dev scope (9d7b9ca, 30bda3b, 19581af).** Dev batches (`SDE_DEV_BATCHES`, or mock runs)
  stay out of the numbers by default. The switch is in the nav on every page.
- **Job labels (5b1553a, 89ab53d, 19787c5).** The UI shows `agent_run` for `rollout`,
  `author_task` for `create` and `analyze_trajectory` for `analyze`. These are display names
  only; the job types and queues keep their identifiers.
- **API for the UI (2d29b06, e209df2).** `GET /v1/tasks/{id}` returns each version's analysis
  (decision, pass review, mutation rules, signals). `GET /v1/tasks` returns each task's family,
  mechanism, environment, batch, accepted version, reuse and per-version history.

**Tradeoffs**
- The UI forwards GETs only, so it can't submit or cancel. The public tunnel URL changes when
  its pod restarts.

## 2026-11-18, 5 AM PST: hill climbing on the production cluster

**What changed**
- **Iterations 0 to 2.** Iteration 0 (`aws-agent-b1`) put 0 of 10 tasks in the band, and every
  scored first version was too easy. Iteration 1 put 0 of 6. Iteration 2 ended with 7 accepted
  of 10 decided (docs/ITERATION_LOG.md).
- **Sliding windows 1 to 9 (f547d7c, be2db66).** Six lanes, one per agent session, each with a
  share of the cluster. Each lane reads its tasks' trajectories and proposes one targeted change
  per finding on its own `lane-*` branch. The manager merges them and deploys with drains
  (docs/HILLCLIMBING.md).
- **Prompts (b9e1d01, 4916619, d1ad21c).** Short, user-voiced instructions that state outcomes
  with no solution cues (342 to 92 words, per the README), comprehensive tests, and world
  commands under about 4 KB so sessions finish instead of stalling.
- **Authoring sessions (66f3046, ab466d2, 9cc727a).** The time boxes are suggested budgets with
  7200 s safety nets. The authors' self-checks are stamped and adopted by `validate` and
  `world_check` when exact (`SDE_ADOPT_AUTHOR_CHECKS`). Image names are per trial.

**Why**
- No prompt produced an in-band task in one shot at the start, so each change had to be judged
  on the tasks authored after it.

**Tradeoffs**
- Windows overlap, so a change's effect is read from the tasks authored after it, not from a
  clean A/B comparison.

## 2026-11-17, 11 PM PST: K3s production cluster on AWS, drained rolling deploys

**What changed**
- **Kubernetes manifests (8d80c73, 2ae7428, 62c9b7a, 4bd5099).** `infra/k8s/` (now `deploy/k8s/`) has a base and
  four overlays: `local` (k3d), `aws`, `aws-prod` (RDS, ElastiCache, S3 through IAM) and
  `aws-prod-mock`. `scripts/k8s-local.sh` runs the local cluster. `scripts/k8s_aws.py` (both now in `deploy/`) runs K3s
  across the EC2 instances over SSM.
- **KEDA (4a8a565, fd5ecf6).** The executor scales on READY and LEASED agent-run jobs in
  Postgres.
- **Chaos test (7256b01).** `scripts/k8s_test.py` kills an executor mid-run, restarts Postgres,
  and checks exactly-once and the KEDA scale-up.
- **Cluster size (af545f8, 40aa1be, 71b6943).** 17 EC2 instances: 9 on-demand and 8 Spot, as one
  K3s server and 16 agents. The AWS Node Termination Handler runs on the Spot nodes. RDS is
  db.t4g.medium. Sizing and cost are in docs/CAPACITY.md.
- **Drained rolling deploys (a2a8a75, de83f9c, 688012f, 3b7a2b7).** On a deploy or a KEDA
  scale-in, a pod stops claiming and lets its work finish: generation sessions for up to 60 min,
  agent runs for up to 3700 s. A Spot interruption hands jobs back at once.
- **Releases (c027b32, eebe8d4, 9805dd4).** Per-release image tags, scoped deploys
  (`--only ui|api|control|…`), and a public HTTPS link to the API through a Cloudflare quick
  tunnel.
- **No total budget cap (4da4386).** `SDE_BUDGET_USD=none` means no cap (the user's decision).
- **Robustness (84cf067, 1081757, 1715ba0).** Scale-downs no longer dead-letter healthy agent
  runs. Workers ride out a database blip instead of crashing. Authoring agents get a restricted
  docker that sees only their own trial's objects.

**Why**
- Drains let a prompt or code change ship while tasks are in flight, without losing or re-paying
  the running sessions. That is what made the sliding-window tuning possible.

**Tradeoffs**
- A full drained deploy can take up to an hour.
- A Spot interruption still costs the interrupted job's work.

## 2026-11-17, 8 PM PST: agent-mode, world-first authoring (plan v2), end to end

The plan is docs/AUTHORING.md; the method is docs/WORLD_CONSTRUCTION.md.
The review findings (R*, N*, L*) and their resolutions are in docs/ARCHITECTURE_REVIEW.md.
Each item lists its main commits.

**What changed**

- **Job types and hand-offs (INF1, 626b4ef).**
  - New `world` and `world_check` jobs, and `create` from a world;
    `after_world` → `after_world_check` → `after_generation` → `after_validate`.
  - World artifacts are content-addressed (`worlds/<sha>/`) in the artifact store, next
    to bundles, drafts and traces.
  - `SDE_AUTHOR_MODE=oneshot|agent`. Agent jobs hold a Docker slot and an hour-long lease.
- **Trace Parser world mutation (EX1, e489e6b, de5d3c3, bebf27d).**
  - Too easy → a `world` mutation job from the parent world; too hard → `revise`
    `easier`; task blamed → `revise` `repair_ambiguity`.
  - Operators come from deterministic rules (`calibration.choose_mutations`; rule names in
    docs/EVALUATION.md); `remove_cues` is always first when the pass review finds cues.
  - Every choice records `{operator, rule, evidence}`. The history travels in job inputs.
- **Gates (TB1, 3601f5c; f5b0175; c369b95).**
  - `validate` gains `wrong_fix` (the bundle's plausible wrong fix must score 0) before
    `leak`.
  - `world_check` builds the world: repro fails twice, repro passes with the fix twice,
    the symptom is visible, the fix isn't one local patch (2+ files, or a dependent the
    wrong fix breaks), and the wrong fix clears the symptom but fails the repro.
  - `SDE_WORLD_HARDNESS = off | warn (default) | enforce | strict`; `result.hardness`
    records the hardness half.
- **Authoring kit (TB2, 272af87, 977d7b1, 7cd990f, 780af13, 87d39e8, c9882a9, 6d231bb).**
  - The `sde-authoring` image, the `sde-world` / `sde-task` self-check CLIs, world scaffolds,
    30/20-min time boxes (later 7200 s safety nets), `run_world_job`.
  - Fixed: Harbor 0.26 puts `/logs/artifacts` under the trial's `artifacts/` (6d231bb).
- **Task author (TB3, f257ca3, 5c9364a, 0376e02, d6ab4b3, 7eefd40, 4baa6ac).**
  - `run_create_job` from a world.
  - The leak review moved into `create`; a leaking draft is repaired, not rejected (N6).
  - The instruction no longer describes the counterexample schedule, the fault-hook
    position or the fix's shape.
- **Tuning (f8c9081, 152b0b6, a124f4a).**
  - Harder worlds and tasks: non-local fixes, external faults, invariant property tests.
  - A semantic and blind leak review (`SDE_LEAK_BLIND`).
  - Every world invariant has a marked test; randomized tests use fixed seeds; the oracle
    runs twice (flake check).
- **Taxonomy v2 and API (API1, e572f4a).**
  - Six families × mechanisms × environments; spec v2 (family, mechanism, shape, dial).
  - `GET /v1/families`; `area` / `mechanism` / `environment` / `dial` on tasks and
    batches; reports by family and mechanism.
- **Per-job dispatch (135b82b).** A create or revise with `world_sha256` runs the agent
  author; anything else runs one-shot, even in agent mode. Repairs keep their world
  through validate.
- **Spend cap (fcfe4c8, 5256d6d).** `SDE_BUDGET_USD` and a per-batch `budget_usd` stop new
  paid claims, using per-job estimates: world $1, create $4, revise $3. The total cap was later
  turned off on AWS (`SDE_BUDGET_USD=none`, see the K3s entry above).
- **Cancel (d8ca8dc).** `POST /v1/tasks/{id}/cancel` and `/v1/batches/{id}/cancel`, a
  `CANCELLED` status that doesn't count as a failure, and the cancelled job's cost kept;
  `scripts/relabel_cancelled.py` relabels earlier stops.
- **Capacity and robustness on AWS and locally.**
  - Per-job Docker slots and `SDE_AGENT_SLOTS` (f8f6e90), and generation on every EC2
    instance (4c7be83).
  - Slot ceilings (d40c9e6, 25d4970), a memory guard, a CPU guard (f743fd1, b31ff23) and
    claim pacing (8a966c8).
  - A bounded Postgres pool, with heartbeats on their own pool (1d5d869, 5f86caa), and
    db.t4g.small (5547aeb; later db.t4g.medium).
  - Parallel S3 uploads (a796fbd).
- **Load-test mode (2e0612c, 25d4970, 53b8a48).** `SDE_MOCK_LLM` runs the real code with
  faked model calls, plus a step driver. It's off by default and never set for a real
  batch.
- **Tests (INT, e2fbfc0, 6272997, 884016c).**
  - Agent-mode e2e scenarios.
  - A real-adapter test across two machines on S3.
  - The cancel and authoring suites (docs/TESTING.md).
- **Docs.** README "The pipeline as built" (now "What it does"); docs/EVALUATION.md;
  docs/archive/ANALYSIS.md §11–12;
  docs/TESTING.md agent mode; docs/DEPLOY.md.

**Fixes found by review or by the live runs**

- Heartbeat errors killed the heartbeat thread, so jobs were run twice (de5d3c3 worker,
  60e561b executor).
- RDS ran out of connections (1d5d869, 5547aeb).
- Suspect starvation: an abandoned job waited for hours on a busy machine (f743fd1).
- The pipeline image on the EC2 instances has no `ps`, so the liveness check failed and an
  agent run went INVALID
  (2161445).
- A dead `world` / `world_check` job didn't reject its task (N1, 02add1e).
- Mutated versions were reported as "initial" (N2, 02add1e).
- A raising authoring function lost its trial (R8, df3929e).
- A failed world job wasn't retried, and "OK" without a world now rejects (R5, R6,
  5525698).
- Follow-ups inherited pins across versions (N4, d436107).
- `review_passes` could raise.
- `compose.dev` shadowed `deploy/local.env` (12d890d).
- `test_cancel` never published its jobs, so it failed only on Redis (94038e1).

**Reverted:** "World mutations use the parent world's hardness checks" (9156191), reverted
in d0130b4, pending the tiebreak version: the user chose tiebreak-only. It's kept on the
`park/hardness-tiebreak` branch (docs/TODO.md).

**Why**
- One-shot drafts were too easy: 58 of 62 scored live runs passed, with none failing
  (docs/archive/ANALYSIS.md §11). The first world-first task (P1) produced a genuine model
  failure (§12.2).

**Tradeoffs**
- An agent-authored task costs about $2.9 against about $0.3 one-shot, and agent sessions
  hold Docker slots for up to 30 min (the time box then; later a 7200 s safety net).
- With the hardness gate at `warn`, a world can still be local; that's recorded, not
  enforced.

## 2026-11-17, 7 PM PST: version cap 8

**What changed**
- `MAX_VERSIONS` defaults to 8 (v1 plus up to 7 revisions), down from 10; `SDE_MAX_VERSIONS`
  still overrides it (the e2e suite pins 3). The PRD and the diagram say v8.

**Why**
- Eight versions leave room for several harder/easier passes (now guided by the Trace
  Parser's review of passing runs) while capping a never-in-band task at ~8× one evaluation.

**Tradeoffs**
- A task that would have reached the band at v9 or v10 is now rejected `exhausted`.

## 2026-11-17, 7 PM PST: batches are back, as a label, with a yield report

**What changed**
- `POST /v1/batches` `{count=10, batch_id?, prompt?|spec?}` creates N tasks (`<batch_id>-tNN`) in
  one transaction; `GET /v1/batches`, `GET /v1/batches/{id}` and `GET /v1/batches/{id}/report`
  list, show and report on them. Every `/v1/tasks` route and `/v1/report` is unchanged.
- `tasks.batch_id` returns as a nullable, indexed label (schema init adds it; SQLite databases
  from the first batch release, where it was NOT NULL, are rebuilt). Single tasks leave it null.
- Every report gains a `yield` block: candidates, functional, accepted, accepted_v1 and
  `learnable_rate` = accepted ÷ finished candidates (null until one finishes).
- `create_task` and `create_batch` share one insert; a batch samples each task's slot against
  every task so far plus its own earlier picks. `scripts/run_tasks.py` uses one batch
  (`--batch`, `--count`, `--prompt`), resumes on rerun and prints the yield.

**Why**
- A batch's yield is the number that gets reported. Ten loose tasks plus a list of ids worked, but the
  batch is the unit we report on, so it should be one call to create and one id to report.

**Tradeoffs**
- Still no batches table and nothing downstream knows about batches: the label only groups
  tasks for status and reporting. Idempotency compares the count (and a shared prompt/spec);
  a sampled batch can't be compared slot by slot, since sampling isn't deterministic.

## 2026-11-17, 5 PM PST: one task per request; no batches

**What changed**
- `POST /v1/batches` and every batch route are gone. `POST /v1/tasks` creates one task from a
  free-text `prompt` (it seeds the standard authoring prompt), an explicit `spec` (taxonomy,
  difficulty, design lines), or nothing (the service samples the least-used taxonomy slot across
  all tasks). An optional `task_id` makes retries safe; reusing it for a different request is 409.
- `GET /v1/tasks` lists tasks; `GET /v1/report` reports over named task ids or everything since a
  time. `tasks.batch_id` is dropped (schema init migrates old databases) along with the queue's
  batch filter, `--batch` flags and the batch store helpers.
- `scripts/run_tasks.py` is the batch run: post N tasks, wait, report on those ids.

**Why**
- The system never needed to group work: every service handles one task's jobs. Batches only
  shaped the API and the report, and pulled sampling, idempotency and status into a concept that
  had no behaviour of its own. One task per request is simpler, and a batch of 10
  is just ten requests whose ids the report is given.

**Tradeoffs**
- Diversity sampling now looks at every task ever created rather than one batch, so it spreads
  across runs too. A report needs the caller's task ids (or a time window) instead of a batch id.
- Dropping a column breaks still-running old code: stop every process before starting the new
  release (no rolling deploy across this change).

## 2026-11-17, 4 PM PST: services on queues; Generation Service and Trace Parser own the loop

**What changed**
- **No central orchestrator; `job_service/` is gone.** The API service only creates tasks
  (one task plus its `create` job per call) and serves reads. Every other component is a service on its
  own queue: a **create** queue for new tasks, a **revise** queue for repairs and revisions, a
  validate queue, the **run** queue (`sde:rollout`) and an `sde:analyze` queue. Each service settles
  its job and enqueues the next stage's job in the same transaction (`docs/PRD.md` §5).
- **Generation Service:** consumes the create and revise queues (one service, revise first),
  drafts or changes a task with GLM-5.1, then hands off to Build + Validate.
- **Trace Parser decides.** Executors upload the whole trace to S3 and always enqueue an
  `analyze` ping. Once a version has five valid runs, the Trace Parser (its own service, not a
  cron batch) extracts evidence and has GLM-5.1 review each failed run, then accepts, puts a
  revision or repair on the revise queue, or rejects. Each task-stage transition has exactly one
  owning service.
- **The trace review moved out of the executor.** `evaluation/trace_parser.py` (deterministic
  metrics and evidence, plus a review that must cite evidence or count as `unexplained`) runs in
  the Trace Parser service once per version, not per agent run inside the executor.
- **Cross-machine trace adoption.** Executors fetch a trace another machine already uploaded before
  running a reclaimed agent run, and upload their own trace the moment Harbor finishes, before
  settling. A worker killed after that point leaves a trial the next owner adopts instead of rerunning.
- **Job type `generate` split into `create` and `revise`,** so new drafts and changes to existing
  tasks have their own queues (and revisions drain first). Both run in the Generation Service;
  `input.origin` (`initial` / `repair` / `revision`) still selects the prompt.

**Why**
- An orchestrator that routed every stage back through one service made it look responsible for
  everything and put slow LLM calls in its decision loop. Direct hand-offs keep each service to one
  job, and they can't be lost, because the hand-off commits with the result.
- Reviewing all five agent runs of a version together is cheaper (one GLM call, not one per failure),
  frees Docker slots sooner, and lets the review compare failures with passes, which is what the
  revision prompt needs.

**Trade-offs**
- The flow rules are spread across the workers instead of living in one file. The stage-ownership
  table in `docs/PRD.md` §5.1 is the single place that lists them.
- Every settled agent run enqueues an `analyze` ping, and only the one that completes the version
  does any work. The rest finish immediately; that's the cost of not needing a central counter.
