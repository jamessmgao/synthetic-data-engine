# Plan: agentic, world-first task authoring

Status: **v2, 2026-11-17** (v1 had one authoring job; v2 splits world building from task
writing from the start). Lead: the task-builder session. This file is the single source
of truth for the work split, the contracts between workstreams, and the merge order.
Contracts change only by editing this file (the lead merges it) and messaging every owner.

Background: [`WORLD_CONSTRUCTION.md`](WORLD_CONSTRUCTION.md) (the method),
[`HARD_TASK_GENERATION_GUIDE.md`](HARD_TASK_GENERATION_GUIDE.md) (families and levers),
[`ANALYSIS.md`](ANALYSIS.md) (why one-shot tasks came out too easy).

## 1. The flow

Every box is one job on the queue. Agent jobs run one Harbor session (terminus-2 +
glm-5.1) in one container and hold one Docker slot; gate jobs are deterministic.

```
POST /v1/tasks ─► world (agent) ─────────────► world_check (gate) ──fail──► world (repair, ≤2)
                  build an executable world,     build the world image;          │
                  state its invariants, run it,  the counterexample's repro      ▼
                  find and record a reproducible must FAIL on the world and   REJECTED (invalid world)
                  counterexample + a world fix   PASS with the world fix
                                                        │ ok
                                                        ▼
                  create (agent, from a world) ─► validate (gates) ──fail──► revise (repair, ≤2)
                  task instruction, tests (the     structural · lint ·
                  counterexample as hidden test),  oracle×2 · nop ·
                  reference fix, wrong fix,        wrong_fix · leak
                  self-check                             │ ok
                                                        ▼
                                                 rollout ×5 (flash) ─► analyze (Trace Parser)
                                                                            │
                     in band → ACCEPTED (task + world published)            │
                     too easy → world (mutation): mutate the WORLD to defeat│the strategy
                                that passed, then world_check → create again│
                     too hard → revise (easier): same world, task-level     │
                                change (localize, fewer interacting reqs)   │
                     task blamed → revise (repair_ambiguity)                ┘
```

- **One world, many tasks** (WORLD_CONSTRUCTION.md Reason D): a checked world can be
  reused by later `create` jobs with a different shape or dial. v1 of the sampler always
  builds a new world; reuse is a later step (§6).
- **Harder means changing the world**, not rewording the task: a mutation job gets the
  parent world, the Trace Parser's evidence and one or two mutation operators.
- One-shot generation (`run_generate_job`) remains the baseline and fallback,
  `SDE_AUTHOR_MODE=oneshot|agent` (default `oneshot` until P1 shows `agent` is better).

### Job types

| Type | Kind | Input | Output | Slot |
|---|---|---|---|---|
| `world` | agent | spec v2, or `{origin: mutation, parent_world_sha256, ...}`, or `{origin: repair, world_sha256, feedback}` | world artifact (`world_sha256`) | Docker |
| `world_check` | gate | `world_sha256` | `{outcome, result: {checks: {build, repro_fails, repro_passes_with_fix}}, feedback}` | Docker |
| `create` | agent | `{origin: initial, world_sha256, shape, dial}` | task bundle (`bundle_sha256`) | Docker |
| `validate` | gate | bundle | gates incl. `wrong_fix`, `leak` | Docker |
| `revise` | agent | `{origin: repair \| easier \| repair_ambiguity, bundle_sha256, world_sha256, ...}` | task bundle | Docker |
| `rollout`, `analyze` | unchanged | | | |

## 2. Work packages and dependency graph

```
                       C0 contracts (this file)
        ┌──────────────┬───────────┼─────────────┬────────────────┐
        ▼              ▼           ▼             ▼                ▼
   TB1 world_check  TB2 world   TB3 task      API1 taxonomy v2,  EX1 Trace Parser:
   + wrong_fix      authoring   authoring     spec v2, submit    mutation decisions,
   gates            kit         kit (from a   creates a world    revise/world inputs,
   (validator)      (image,     world) +      job first          history in job rows
        │           sde-world,   revision                │         (stubs for new types)
        │           run_world   instructions            │                │
        │           _job)           │                   │                │
        │              └──────┬─────┘                   │                │
        │                     ▼                         │                │
        │        INF1 job types world / world_check / create-from-world;  │
        │        Docker slots for agent jobs; world artifacts in the     │
        │        artifact store (S3); authoring image in deploy          │
        │                     │                         │                │
        └─────────────────────┴────────────┬────────────┴────────────────┘
                                           ▼
            P1 prototype: 3 worlds → 3 tasks, outside the pipeline (needs TB1-3 only)
                    spend ~$8-15, user approval required
                                           ▼
            INT e2e with SDE_AUTHOR_MODE=agent  →  P6 batch of 10+ (approval)
```

| ID | Package | Owner | Depends on | Spend |
|---|---|---|---|---|
| C0 | Contracts, this file | task-builder (lead) | – | $0 |
| TB1 | `gate_world_check` (new) and `gate_wrong_fix` in `validator/` | task-builder | C0 | $0 |
| TB2 | World authoring kit: `authoring/` image, `sde-world` self-check CLI, world instruction template (family briefs from the guide), `run_world_job` | task-builder | C0 | $0 |
| TB3 | Task authoring kit: task instruction template (from a world), `run_create_job`, `sde-task` self-check, mutation/revision instructions | task-builder | C0, TB2 world format | $0 |
| TB4 | Pull in task-difficulty's `inspect_task.py` checks (`audit`, `leaks`, `explain`) as gates/tools where they add to the leak gate | task-builder | -75 commits them | $0 |
| API1 | Taxonomy v2, sampler, spec v2, `POST /v1/tasks` creates the task and its **`world`** job; reports by family/mechanism | api-service | C0 | $0 |
| INF1 | Job types `world`, `world_check`, `create` from a world; Docker-slot accounting and hour-long leases for agent jobs; world artifacts through the artifact store (put/get world, and drafts); handoffs `world → world_check → create → validate`; authoring image in deploy | infrastructure | C0; TB1-3 signatures (stubs fine) | $0 |
| EX1 | Trace Parser decisions: too easy → `world` mutation job (operators by deterministic rules, §3.6); too hard → task-level `revise` (easier); history in job rows; verdicts/reports show worlds, `world_check`, `wrong_fix` | executor-worker | C0; stubs for TB1, INF1 | $0 |
| P1 | Prototype: 3 worlds (distributed, database, algorithmic) → 3 tasks, vs one-shot | task-builder | TB1-3 | ~$8-15, **approval** |
| INT | e2e with `SDE_AUTHOR_MODE=agent` | executor-worker | INF1, EX1, API1, TB1-3 | ~$2-5 smoke, **approval** |
| P6 | Batch of 10+ | task-builder + api-service | INT | ~$20-40, **approval** |

Parallel from now: TB1, TB2, API1, EX1 (against stubs), INF1 (against signatures).

## 3. Contracts

### 3.1 Spec v2 (sampler output, the `world` job's `input.spec`)

```json
{"family": "database_consistency", "mechanism": "isolation_anomaly", "shape": "fix",
 "environment": "Python service", "verification": "fault_injection", "dial": 2,
 "seed": "optional free-text world idea", "design": ["optional extra lines"]}
```

Uniqueness for diversity: `(family, mechanism, environment)`. Old `{taxonomy, difficulty}`
specs stay valid for one-shot mode.

**Batch submission by software area.** `POST /v1/batches` (and `sde run`) takes an
optional `area`: a family name from §3.2 (e.g. `"database_consistency"`) or a list of
them. With an area, every task in the batch is sampled within it: mechanism, environment,
verification and dial are random (least-used first) inside the area. Without one, the
whole taxonomy is sampled (all random). Optional finer filters: `mechanism`,
`environment`, `dial`. An unknown area is a 422 listing the valid ones. `GET /v1/families`
(or the batch error) lists the areas and their mechanisms.

```json
POST /v1/batches  {"count": 10, "area": "database_consistency"}
POST /v1/batches  {"count": 10, "area": ["compilers_parsers", "algorithmic_constraints"]}
POST /v1/batches  {"count": 10}                              # all random
```

### 3.2 Taxonomy v2 values

Owned by API1 (`shared/taxonomy/taxonomy.py`), from the guide's six priority families:

| family | mechanisms | environments | verification |
|---|---|---|---|
| `distributed_concurrency` | `adversarial_schedule`, `partial_failure_recovery`, `lost_acknowledged_write`, `linearizability_violation` | Python service, Go service | `deterministic_scheduler`, `fault_injection`, `model_check_small` |
| `algorithmic_constraints` | `greedy_counterexample`, `global_constraint_interaction`, `optimality_required` | Python service, Go service, C program | `reference_solver`, `exhaustive_small`, `property_based` |
| `performance_resources` | `scalability_wall`, `memory_bound`, `exact_semantics_under_speedup` | Python service, Go service, C program, Java application | `benchmark_with_margin`, `differential` |
| `database_consistency` | `isolation_anomaly`, `write_skew`, `crash_between_commit_and_effect`, `idempotent_retry` | Python service (SQLite), PostgreSQL database | `fault_injection`, `invariant_check`, `concurrent_integration` |
| `compilers_parsers` | `semantics_changing_transform`, `aliasing_side_effects`, `stateful_incremental_parse`, `fragmented_frames` | Python service, Go service | `differential_execution`, `grammar_fuzzing`, `reference_interpreter` |
| `cross_component_stateful` | `multi_step_causal_failure`, `retry_cache_commit_interaction`, `compensation_after_irreversible_effect`, `misleading_symptom` | Python service, Go service | `state_machine_test`, `fault_injection`, `integration` |

Shape default: counterexample/anomaly/failure mechanisms → `fix`; `optimality_required` →
`implement`; `scalability_wall`, `memory_bound` → `optimize`.

### 3.3 World artifact (TB2 writes, INF1 stores, everyone reads)

Content-addressed: `worlds/<world_sha256>/` (through the artifact store; S3 on AWS):

```
worlds/<sha>/
├── environment/          # Dockerfile + the world's code, harness, fault injection (agent-visible later)
├── world_fix/fix.sh      # makes the invariants hold; proves the counterexample is fixable
├── repro/repro.sh        # deterministic reproduction; exit 1 = invariant violated, 0 = holds
└── world.json
```

```json
{"spec": {...spec v2...},
 "invariants": ["confirmed reservations never exceed stock", "..."],
 "counterexample": {"sequence": ["A reads stock=1", "B reads stock=1", "..."],
                    "reproduce": "repro/repro.sh", "observed": "2 confirmed, stock 1"},
 "plausible_wrong_fix": "check-then-insert inside one transaction at READ COMMITTED",
 "lineage": {"parent_world_sha256": null, "mutations": []}}
```

`world_sha256` hashes every file, like bundles. The `mutations` list is written by the
world author from the mutation job's input (§3.6); nobody edits a world in place.

### 3.4 Agent job functions (TB2/TB3 provide, INF1 calls)

```python
# generator/author.py
run_world_job(job_id, job_input, work_dir=None) -> dict
#   -> {outcome, error, cost_usd, result: {world_sha256, world_path, name, authoring: {...}}}
run_create_job(job_id, job_input, work_dir=None) -> dict
#   same shape as run_generate_job, plus result.world_sha256
```

Both render an authoring Harbor task under `work/authoring/<job_id>/`, run terminus-2 +
`openrouter/z-ai/glm-5.1` (agent timeout 60 min), collect the outputs from the trial's
`/logs/artifacts/`, validate them, and write content-addressed artifacts. Idempotent per
`job_id`: a finished authoring trial in the job's trace dir is adopted. Cost is the
trial's `agent_result.cost_usd`. `revise` jobs in agent mode call `run_create_job` with
the revise input (§3.6).

### 3.5 Gates (TB1)

```python
# validator/gates.py
run_world_check_job(world_dir, trace_dir) -> {outcome, failed_check, result: {checks}, feedback, cost_usd}
```

`world_check`: build `environment/`; run `repro/repro.sh` against the world (must exit 1);
apply `world_fix/fix.sh`, rerun (must exit 0); twice each to catch flakiness. Feedback goes
to a `world` repair job.

`validate` gains `wrong_fix` after `nop` (the bundle's `solution/wrong_fix.sh`, from
`TaskDraft.wrong_fix_script`, run as the oracle must score 0 with a clean verdict;
skipped when absent, required in agent mode), then `leak`. Result keys
`result.gates.wrong_fix`, `result.gates.leak`.

### 3.6 Decisions and inputs written by the Trace Parser (EX1)

- **Too easy** → `world` job `{origin: "mutation", parent_world_sha256, spec, mutation:
  {operators: [...], why}, mutations: [...prior history + this entry], analysis,
  target_fix}`. Then `world_check` → `create` from the new world.
- **Too hard** → `revise` `{origin: "easier", bundle_sha256, world_sha256, evidence, ...}`
  (same world, task-level change).
- **Task blamed** → `revise` `{origin: "repair_ambiguity", ...}`.
- Operators (WORLD_CONSTRUCTION.md §8): `add_concurrent_execution`,
  `inject_partial_failure`, `introduce_global_invariant`, `generate_counterexample`,
  `tighten_resource_bound`, `add_persistent_state`, `extend_cross_component_dependency`,
  `add_semantic_edge_case`, `add_realistic_distractor`, `compose_failure_modes`,
  `expand_state_space`, `introduce_temporal_constraint`, and `remove_cues`.
- **`remove_cues`**: rewrite the cue sentences the pass review cited (instruction or code
  text pointing at the fix) at the interface level; the world stays the same. Cues mean
  the task leaks, so it is always the first pick when the pass review found any.
- **Chosen by deterministic rules first** from trace metrics and the pass review
  (`fix_applied`, `simple_strategy`, files written): process-local/in-memory fix →
  `add_persistent_state`; one file edited where the symptom shows →
  `extend_cross_component_dependency`; greedy/first-fit strategy passed →
  `generate_counterexample`; the pass review's `suggestions[].technique` only breaks ties.
- **History lives in job rows**: the chosen operators and reason go in the analyze job's
  `result.mutation`; the full history travels in the next job's input; no shared file is
  read-modify-written. `result.mutation.rules` records `{operator, rule, evidence}` for each
  chosen operator, so we can measure which rules move the pass rate.

## 4. Ownership and merge protocol

| Area | Owner |
|---|---|
| `generator/`, `validator/`, `authoring/`, `experiments/`, `docs/` (except PRD/TESTING) | task-builder |
| `shared/taxonomy/taxonomy.py`, `api/submit.py`, `api/` | api-service (also owns the task-difficulty worktree) |
| `shared/jobs/worker.py`, `shared/jobs/handoffs.py`, `shared/contracts/models.py` job types, `shared/storage/`, `deploy/` | infrastructure |
| `evaluation/`, `tests/e2e/`, `tests/shared/jobs/test_services.py` | executor-worker (integrates `main`) |

- Branch from `origin/main`; never edit another owner's files; send the change instead.
- Push and send the sha to executor-worker (the integrator) and the lead. The integrator
  merges in dependency order and runs `python -m pytest -q tests evaluation generator api`
  plus e2e before pushing main.
- Merge order: C0 → TB1, API1 → TB2 → TB3 → INF1 → EX1 → INT.
- No paid runs (P1, INT smoke, P6) without the user's approval, relayed by the lead.
- task-difficulty's experiments stay committed on their own branch; only what TB4 lists
  moves.

## 5. Status

| ID | Status |
|---|---|
| C0 | v2 done |
| TB1-TB3 | in progress (task-builder) |
| TB4 | done: cherry-picked 2c4c091 (`experiments/inspect_task.py`: audit, leaks, explain; 7 pass, 1 skips without the tla1 bundle) |
| API1 | go given by the user (relayed by the lead) |
| INF1 | go given |
| EX1 | go given; rebase onto v2 |
| P1, INT, P6 | waiting (approval / dependencies) |

## 6. Later

- World reuse: the sampler picks an existing checked world for a new task with a
  different shape or dial.
- Checkpointed authoring: resume a timed-out world session from its last artifact.
