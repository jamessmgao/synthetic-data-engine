# Authoring: world-first, agentic task authoring (as built)

How synthetic-data-engine authors a task: an agent first builds an executable **world** (a
small system with a real defect), a deterministic gate checks it, and a second agent writes the
**task** from it. Too-easy tasks are made harder by changing the world, not the wording.

This replaces the plan of 2026-11-17 (`AUTHORING.md`). Section numbers §1, §2 and
§3.1–3.6 are kept, because code comments cite them.

Background:
- [WORLD_CONSTRUCTION.md](WORLD_CONSTRUCTION.md): the method (worlds, invariants,
  counterexamples, mutation operators);
- [DIFFICULTY.md](DIFFICULTY.md): the families, their levers, and what makes a task hard;
- [VALIDATION.md](VALIDATION.md): every gate a task must pass.

## 1. The flow

Every box is one job on the queue. Agent jobs run one Harbor session (terminus-2 + glm-5.1) in
the `sde-authoring` image and hold one Docker slot; gate jobs are deterministic.

```
POST /v1/batches ─► world (agent) ─► world_check (gate) ──fail──► world repair (≤2) ─► … ─► REJECTED invalid
  new world:        build the system,  build; repro fails ×2;
  spec v2           state invariants,  fix passes ×2; hardness
  reuse_worlds:     record a           checks (warn mode)
  a variant of a    counterexample,          │ ok
  checked world     a fix, a wrong fix       ▼
                                      create (agent, from the world) ─► validate (gates) ──fail──► repair (≤2)
                                      instruction, tests, reference      structural · lint · oracle ×2 ·
                                      solution, wrong fix; its own        nop · wrong_fix · leak
                                      self-check and leak review                │ ok
                                                                                ▼
                                                     5 agent runs (terminus-2 + glm-5.3-flash, high)
                                                                                │  early stop once the verdict is certain
                                                                                ▼
                                                                  analyze (Trace Parser)
       1-3 of 5 passed, no failure blamed on the task ─► ACCEPTED (task + world published)
       4-5 of 5 (too easy) ─► world (mutation): change the WORLD to defeat what passed
       0 of 5 (too hard)   ─► revise (easier): same world, a task-level change
       a failure blamed on the task ─► revise (repair_ambiguity), at any pass count
       still out of band at version 8 ─► REJECTED exhausted
```

- **Mode per job.** `SDE_AUTHOR_MODE=agent` runs this flow. A `create` or `revise` whose input
  has `world_sha256` runs the agent author (`run_create_job`); anything else runs the one-shot
  generator (`run_generate_job`, one strict-JSON glm-5.1 call), the baseline and the fallback
  for raw prompts and v1 specs. Switching modes is safe for tasks in flight.
- **Harder means changing the world.** A mutation job gets the parent world, the Trace
  Parser's pass review, the passing agent runs' condensed traces, the parent's hardness results
  and 1–2 mutation operators (§3.6).
- **One world, more tasks.** `reuse_worlds` starts a task from an existing checked world as a
  **variant**: the old defect is fixed and one new defect of a reseeded mechanism is planted
  (§4).
- **Self-checks first.** The world author must pass `sde-world check` (the gate's own code)
  before it finishes; the task author runs `sde-task run` (oracle, nop, wrong fix, in parallel)
  and the leak review before returning. A failing or leaking draft goes straight to
  `repair<k+1>`. With `SDE_ADOPT_AUTHOR_CHECKS=1`, `validate` and `world_check` adopt the
  author's stamped results when they match the artifact byte for byte.

### Job types

| Type | Kind | Input | Output |
|---|---|---|---|
| `world` | agent | spec v2; or `{origin: mutation, parent_world_sha256, mutation, …}`; or `{origin: variant, parent_world_sha256, variant_of, spec, mutation, earlier_instructions}`; or `{origin: repair, world_sha256, feedback}` | a world (`world_sha256`) |
| `world_check` | gate | `world_sha256` | `{outcome, result: {checks, hardness}, feedback}` |
| `create` | agent | `{origin: initial, world_sha256, shape, dial}` | a task bundle (`bundle_sha256`) |
| `validate` | gate | a bundle | `result.gates` incl. `wrong_fix`, `leak` |
| `revise` | agent | `{origin: repair \| easier \| repair_ambiguity, bundle_sha256, world_sha256, …}` | a task bundle |
| `rollout` (agent run), `analyze` | | see [EVALUATION.md](EVALUATION.md) | |

## 2. How it was built

Built on 2026-11-17 as parallel work packages against the contracts in §3, then tuned in the
production hill climb ([HILLCLIMBING.md](HILLCLIMBING.md), [ITERATION_LOG.md](ITERATION_LOG.md)).

| ID | Package | Commits |
|---|---|---|
| TB1 | `gate_wrong_fix`, `shared/taskformat/world_check.py` (a derived check image, no bind mounts) | 3601f5c |
| TB2 | the `authoring/` image, `sde-world` / `sde-task`, `generator/authoring_harness.py`, `run_world_job`, `prompts/world.md`, `families.py` | 272af87, 7cd990f, a796fbd |
| TB3 | `generator/author_task.py` `run_create_job` (all origins), `prompts/task_author.md`, `prompts/world_mutation.md` | f257ca3, 0376e02 |
| TB4 | `experiments/inspect_task.py` (audit, leaks, explain) | 4f86fd0 |
| API1 | taxonomy v2, the family-first sampler, spec v2, batches by `area` | e572f4a |
| INF1 | `world` / `world_check` jobs, handoffs, world artifacts, Docker slots | 626b4ef |
| EX1 | too easy → world mutation; deterministic operator rules recorded as `{operator, rule, evidence}` | e489e6b, 3a8d040 |

**P1** (the first prototype, local, 2026-11-17; `scripts/p1_prototype.py`): one world in
$0.51 / 5.4 min (18 steps), world_check OK; the task in $2.39 / 21.6 min, validate OK. Agent
run 1 was `VALID_FAIL` (the agent changed the documented crash hook); agent run 2 stalled on a
model call. It found the Harbor 0.26 artifacts-path bug (fixed in 6d231bb).

## 3. Contracts

### 3.1 Spec v2 (the sampler's output, a `world` job's `input.spec`)

```json
{"family": "performance_resources", "mechanism": "lsm_compaction_pathology", "shape": "optimize",
 "environment": "Java application", "verification": "differential", "dial": 3,
 "domain": "file_sync_client", "domain_label": "file sync client (local and remote replicas)",
 "mechanism_note": "compaction re-merges every run on each flush; …"}
```

- **Diversity** is spread over `(family, mechanism, environment)` and the application domain:
  the family with the least weighted use first, then an unused (mechanism, environment) with
  the least-used values; environments spread within a family over the live window.
- **Domains:** 40 application domains (`shared/taxonomy/taxonomy.py` `APP_DOMAINS`), plus compiler
  domains for `compilers_parsers`. Performance worlds never draw `payment_ledger`.
- **`mechanism_note`** (`MECHANISM_NOTES`) is the defect pattern as the world author should
  plant it, with the guarantee to state as an outcome; `world.md` and `world_variant.md`
  render it.
- Old `{taxonomy, difficulty}` specs stay valid for one-shot mode.

**Batches by software area.** `POST /v1/batches` takes an optional `area` (one family or a
list); every task in the batch is sampled inside it. Optional finer filters: `mechanism`,
`environment`, `dial`. An unknown area is a 422 listing the valid ones; `GET /v1/families`
lists them.

```json
POST /v1/batches  {"batch_id": "b1", "count": 5, "area": ["performance_resources"], "dial": 3}
POST /v1/batches  {"batch_id": "b2", "count": 3, "reuse_worlds": true, "area": ["distributed_concurrency"]}
```

With `reuse_worlds: true` the batch takes only `area` (the world brings its spec; §4).

### 3.2 Taxonomy v2

`shared/taxonomy/taxonomy.py` (`FAMILIES`, `FAMILY_WEIGHTS`). Retired on 2026-11-18:
`greedy_counterexample` and `optimality_required` (a greedy-vs-optimal defect names its
textbook remedy: 4 leaks), and `scalability_wall` and `memory_bound` (generic; their tasks
leaked or came in 5/5).

| Family | Weight | Mechanisms | Environments | Verification |
|---|---|---|---|---|
| `distributed_concurrency` | 1.0 | adversarial_schedule, partial_failure_recovery, lost_acknowledged_write, linearizability_violation, snapshot_dedup_skew, fencing_check_granularity, lease_skew_direction, session_index_without_term, reconfig_quorum_early_switch, tenant_key_dropped_on_replay, dedup_watermark_from_truncated_suffix | Python service, Go service | deterministic_scheduler, fault_injection, model_check_small |
| `algorithmic_constraints` | 1.0 | global_constraint_interaction, incremental_dp_stale_predecessor, bound_inadmissible_with_group_cost, shared_capacity_across_stages, interval_tree_rotation_maxend_order, tie_break_lost_on_incremental_update, unsound_dominance_pruning, residual_not_cancelled_on_capacity_decrease, local_tiebreak_not_global_lexicographic, state_key_omits_rare_component, wraparound_boundary_in_sweep, rollback_order_with_path_compression | Python service, Go service, C program | reference_solver, exhaustive_small, property_based |
| `performance_resources` | 1.5 | exact_semantics_under_speedup, late_reference_retention, coarse_invalidation_recompute, lsm_compaction_pathology, cross_boundary_quadratic, offset_pagination_rescan, stampede_on_shared_miss, hot_key_shard_skew, defensive_copy_churn, unbounded_queue_collapse, group_commit_durability, adversarial_hash_degradation, lock_convoy_across_io, backtracking_in_tokenizer, stable_tiebreak_parallel_merge, reassociation_drift | Python service, Go service, C program, Java application | benchmark_with_margin, differential |
| `database_consistency` | 1.0 | isolation_anomaly, write_skew, crash_between_commit_and_effect, idempotent_retry, phantom_range_check, mvcc_gc_vs_long_reader, derived_state_rollback_drift, session_read_your_writes | Python service (SQLite), PostgreSQL database | fault_injection, invariant_check, concurrent_integration |
| `compilers_parsers` | 1.0 | semantics_changing_transform, aliasing_side_effects, stateful_incremental_parse, fragmented_frames, error_recovery_state_leak, constant_fold_semantics_mismatch, macro_hygiene_capture | Python service, Go service | differential_execution, grammar_fuzzing, reference_interpreter |
| `cross_component_stateful` | 1.0 | multi_step_causal_failure, retry_cache_commit_interaction, compensation_after_irreversible_effect, misleading_symptom, cache_fill_race_divergence, protocol_wrong_state_accept, saga_timeout_double_compensation, wall_clock_ordering_skew | Python service, Go service | state_machine_test, fault_injection, integration |

Shapes: every performance mechanism → `optimize`; a mechanism with "schema" → `migrate`;
everything else → `fix`. Dials: 1–3 (3 = hardest; production runs dial 3).

### 3.3 World artifact

Content-addressed: `worlds/<world_sha256>/` in the artifact store (S3 on AWS). Nobody edits a
world in place; `world_sha256` hashes every file.

```
worlds/<sha>/
├── environment/              Dockerfile + the system (agent-visible in the task)
├── repro/repro.sh            deterministic reproduction: exit 1 = an invariant is violated, 0 = holds
├── repro/symptom_check.sh    the symptom an agent would try first (a hardness file)
├── repro/dependent_check.sh  optional: a dependent a one-file fix breaks
├── world_fix/fix.sh          makes every invariant hold
├── world_fix/wrong_fix.sh    the plausible wrong fix, implemented (a hardness file)
└── world.json
```

```json
{"spec": {"…": "spec v2"},
 "invariants": ["…"],
 "counterexample": {"sequence": ["…"], "reproduce": "repro/repro.sh", "observed": "…"},
 "plausible_wrong_fix": "…",
 "lineage": {"parent_world_sha256": null, "variant_of": null,
             "mutations": [{"operators": ["…"], "defeats": "the strategy that passed, and why it now fails"}]}}
```

The pipeline writes `lineage` (the parent, `variant_of`, and every mutation so far); a
mutation's author records only what it `defeats`.

### 3.4 Agent job functions

```python
# generator/author.py
run_world_job(job_id, job_input, work_dir=None) -> dict
#   -> {outcome, error, cost_usd, result: {world_sha256, world_path, name, authoring: {...}}}
# generator/author_task.py
run_create_job(job_id, job_input, work_dir=None) -> dict
#   the same shape as run_generate_job, plus result.world_sha256
```

Both render an authoring Harbor task, run terminus-2 + `openrouter/z-ai/glm-5.1` in the
`sde-authoring` image, collect `/logs/artifacts/`, validate them and write content-addressed
artifacts. Idempotent per `job_id`: a finished authoring trial in the job's trace dir is
adopted (never a cancelled or output-less one).

- **Prompts** (`generator/prompts/`):
  - `world.md`: a new world;
  - `world_mutation.md`: a too-easy world made harder, with `$family_brief`;
  - `world_variant.md`: spliced into `world_mutation.md` for a variant (its opening and its
    "variant to make" section);
  - `task_author.md`: a task from a world.
- **Time boxes:** `SDE_WORLD_TIMEOUT_S` and `SDE_CREATE_TIMEOUT_S` (both 7200 s by default,
  `generator/author.py`, `generator/author_task.py`) are a safety net for a hung session; the
  prompts aim for about 12 minutes. One model call is capped by `SDE_AUTHORING_LLM_TIMEOUT_S`
  (`SDE_WORLD_LLM_TIMEOUT_S` for world jobs; unset = litellm's default; set to 600 s / 1200 s on
  AWS, 09821ad). `SDE_AUTHORING_REASONING_EFFORT` / `SDE_WORLD_REASONING_EFFORT` set the reasoning
  effort (world jobs high on AWS).
- **Shared Docker daemon:** sessions on one generation pod share its dind daemon. Dev builds
  are tagged per job (`sde-dev-<job id>`), never a shared name; a shared tag let sessions
  overwrite each other's image on 2026-11-19.
- **`revise` in agent mode** calls `run_create_job` with the revise input (§3.6).

### 3.5 Gates

```python
# shared/taskformat/world_check.py
run_world_check_job(world_dir, trace_dir) -> {outcome, failed_check, result: {checks, hardness}, feedback, cost_usd}
```

- **`world_check`**, stopping at the first failure:
  - structure;
  - build (the base image pinned to its digest);
  - `repro.sh` exits 1, twice;
  - after `fix.sh`, `repro.sh` exits 0, twice.

  Then the hardness checks run (`symptom_visible`, `symptom_gone_with_fix`, `fix_not_local`,
  `wrong_fix_survives`, and the warning `limit_named`). `SDE_WORLD_HARDNESS=warn` (the default)
  records them in `result.hardness` without failing the world; `enforce` fails on the first
  one. Feedback goes to a world repair job (≤2).
- **`validate`**: structural, lint, oracle ×2, nop, `wrong_fix` (the bundle's
  `solution/wrong_fix.sh` must score 0; required in agent mode), then `leak`:
  - a semantic review against the hidden fix and the world;
  - a blind prediction of the fix from the instruction alone;
  - a judge of that prediction, where only a `specific` match is a leak.

  Results are in `result.gates.<name>`. Every gate and the cases it caught:
  [VALIDATION.md](VALIDATION.md).

### 3.6 Decisions and inputs written by the Trace Parser

`evaluation/trace_parser_service.py` decides each version once (a compare-and-set on the
task's stage), when all 5 slots are scored or early stopping finds the verdict certain.

- **Too easy** → a `world` job:

  ```
  {origin: "mutation", parent_world_sha256, spec,
   mutation: {operators, why}, mutations: [history…, this entry],
   analysis, target_fix, solver_runs, hardness}
  ```

  Then `world_check`, then `create` from the new world.
  - `analysis` is the pass review: why it was easy, the strategy that passed, cues and
    suggestions.
  - `solver_runs` holds the passing agent runs' condensed traces (the world author only; the
    task author never sees them).
  - `hardness` is the parent's hardness results.
- **Too hard** → `revise {origin: "easier", bundle_sha256, world_sha256, evidence, …}`: the
  same world, a task-level change.
- **Task blamed** → `revise {origin: "repair_ambiguity", …}`.
- **Operators** (WORLD_CONSTRUCTION.md §8): `add_concurrent_execution`,
  `inject_partial_failure`, `introduce_global_invariant`, `generate_counterexample`,
  `tighten_resource_bound`, `add_persistent_state`, `extend_cross_component_dependency`,
  `add_semantic_edge_case`, `add_realistic_distractor`, `compose_failure_modes`,
  `expand_state_space`, `introduce_temporal_constraint`, and `remove_cues`.
- **How operators are chosen** (`evaluation/calibration.py` `choose_mutations`, fixed rules,
  no model), in this order:
  1. `remove_cues` first when the pass review found cues. It rewrites the instruction or code
     sentences that point at the fix and doesn't count toward the limit.
  2. For performance worlds, `extend_cross_component_dependency` (`family_cross_first`).
  3. The pass review's first suggestion (`suggestions[].technique` → operator).
  4. `one_file_patch` (the passing runs edited one file) → `extend_cross_component_dependency`.
  5. Keyword rules on the review's `fix_applied` / `simple_strategy` and on the code the
     passing runs wrote: greedy/heuristic → `generate_counterexample`; a lock or reorder →
     `add_concurrent_execution`; a process-local guard → `inject_partial_failure`; a
     self-checking agent → `introduce_global_invariant`; timing → `introduce_temporal_constraint`;
     a special case → `add_semantic_edge_case`.
  6. `straight_to_the_fix` (≤8 steps, no cues) → `add_realistic_distractor`.
  7. The review's other suggestions.
  8. The defaults, `introduce_global_invariant` and `generate_counterexample`.

  At most `MAX_MUTATIONS` (2) difficulty operators per mutation. Operators already applied to
  the world go last. `add_persistent_state` is a last resort: 0 of 7 such mutations landed in
  band.
- **History lives in job rows:** the analyze job's `result.mutation` holds the operators and
  `rules: [{operator, rule, evidence}]`; the full history travels in the next job's input.
  Nothing shared is read-modify-written.

## 4. World reuse and variants

`reuse_worlds: true` (`api/submit.py`, `shared/taxonomy/world_selection.py`) starts each task
from a checked world instead of building one.

- **Which world** (`select_worlds`):
  - **Excluded:** worlds with an accepted task, a leak rejection under the new prompts, a
    blind-only leak, or a failed world job or check; worlds reused `MAX_REUSES` (2) times; and
    greedy/heuristic worlds.
  - **Preferred tiers, in order:** near (its best task scored 4/5), hard (0/5), unscored,
    old_leak, easy (5/5).
  - **Domain cap:** a domain stays at or under 20% of the live window (lifted for
    single-family requests).
- **The new spec** (`reseed_spec`): the same family and environment, a different mechanism
  (one the world's lineage hasn't had, least used first; never `greedy_counterexample`), the
  world's own domain.
- **The variant** (`origin: "variant"`, two operators rotated by task id from
  `generate_counterexample`, `extend_cross_component_dependency`,
  `introduce_temporal_constraint`, `compose_failure_modes`):
  1. remove the existing defect by applying the parent's fix;
  2. plant ONE new defect of the reseeded mechanism in a different function or component;
  3. write new invariants, counterexample, fix and wrong fix;
  4. check that the parent's `fix.sh` does not make it correct.

  `earlier_instructions` (up to 3 tasks already written on the world) are shown so the new
  task differs. The world records `lineage.variant_of`.

On 2026-11-19 at 00:01 the user stopped variants ("new worlds only"); the code path remains.

## 5. Settings

| Setting | Default | Meaning |
|---|---|---|
| `SDE_AUTHOR_MODE` | `oneshot` | `agent` = this flow |
| `SDE_AUTHORING_IMAGE` | `sde-authoring:dev` | the authoring image |
| `SDE_WORLD_HARDNESS` | `warn` | `off` / `warn` / `enforce` |
| `SDE_ADOPT_AUTHOR_CHECKS` | `0` | `1` = gates adopt the authors' stamped self-checks |
| `SDE_LEAK_REVIEW` / `SDE_LEAK_BLIND` | 1 / 1 | the leak review and its blind prediction |
| `SDE_WORLD_TIMEOUT_S` / `SDE_CREATE_TIMEOUT_S` | 7200 / 7200 | authoring session safety nets (the prompts aim for ~12 min) |
| `SDE_MAX_VERSIONS` | 8 | versions before REJECTED exhausted |

Every setting the code reads is listed in `deploy/local.env.example` (enforced by
`tests/deploy/test_compose_env.py`).
