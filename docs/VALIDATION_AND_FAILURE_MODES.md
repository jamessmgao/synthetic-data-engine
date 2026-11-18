# How synthetic-data-engine validates tasks and agent runs, and why agents fail

This doc does two things:
- it walks through every check a task passes between "a world is built" and "a task is
  accepted";
- it takes six accepted tasks from the production cluster and shows how each one is hard,
  and why the pinned solver (terminus-2 + glm-5.3-flash, reasoning high) failed it.

**Dashboard links** use the current Cloudflare tunnel,
https://librarian-anyone-zealand-lee.trycloudflare.com (`make aws-k8s-public` prints the
current one). The tunnel URL changes on every dashboard deploy; the paths after it don't:
- `/tasks/<task id>`: a task, its versions, gates, leak review and agent runs;
- `/artifacts/traces/<job id>`: a job's trace (an agent run's trajectory);
- `/artifacts/worlds/<sha>`: a world;
- `/artifacts/bundles/<sha>`: a task bundle.

Failure attributions below are the Trace Parser's, reported as given.

---

## 1. The checks at a glance

| Stage | Check | Code | What it catches |
|---|---|---|---|
| World | structure, build | `shared/taskformat/world_check.py:run_world_check_job` | a missing `world.json` / repro / fix, a Dockerfile that doesn't build |
| World | `repro_fails` ×2, `repro_passes_with_fix` ×2 | same | a counterexample that isn't real, deterministic or fixable |
| World | hardness (`SDE_WORLD_HARDNESS`, `warn` today) | `world_check._hardness` | a symptom that doesn't show; a one-file fix; a **plausible wrong fix** that doesn't fool the symptom check, or that passes the real repro |
| Authoring | the author's own self-check | `sde-task check` / `sde-task run --as all` (`authoring/`) | schema, invariant markers, fixed seeds; oracle, nop and wrong fix run before the draft is handed in |
| Authoring | **semantic leak review** + **blind prediction** | `validator/gates.py:gate_leak` (run at create, adopted by validate) | an instruction that states the fix, its location, its mechanism or the failing sequence |
| Validate | `structural` | `gates.gate_structural` → `validator/validate.py` | bad layout, a `task.toml` that fails Harbor's schema |
| Validate | `lint` (static) | `gates.gate_lint` (`harbor lint --select ALL`) | blocking lint rules (`config.LINT_BLOCKING_RULES`) |
| Validate | `oracle1`, `oracle2` | `gates.gate_oracle` | `solution/solve.sh` must score reward 1, twice (a flaky test fails one) |
| Validate | `nop` | `gates.gate_nop` | the untouched environment must score exactly 0: the tests really detect the bug |
| Validate | `wrong_fix` | `gates.gate_wrong_fix` | `solution/wrong_fix.sh` (the world's plausible wrong fix) must score exactly 0: the tests reject the tempting fix |
| Agent runs | classify | `evaluation/classify.py:classify` | timeouts, Harbor or environment errors, a wrong agent config → INVALID (never scored, retried up to 3 times) |
| Agent runs | failure review (per failed run) | `evaluation/trace_parser.py`, `trace_parser_service.py:analyze` | whether each failure is the **model's** fault or the **task's** |
| Agent runs | pass review (too-easy versions) | `trace_parser.review_passes` | why it was easy: the cues, the simple strategy that worked |
| Agent runs | decide + mutate | `evaluation/calibration.py`, `choose_mutations` | accept / mutate the world / ease / repair / reject |

A failed gate never just rejects: the evidence goes back to the author as a repair, up to 2
per version (`shared/jobs/handoffs.py:after_world_check`, `after_generation`, `after_validate`).
A version therefore gets at most 3 drafts of its world and 3 of its task.

---

## 2. World checks: is the bug real, and is the wrong fix tempting?

A world is a small system (200–500 lines) with a defect. Its `world.json` holds:
- the **invariants**: the outcomes that must hold, e.g. "a charge credits the wallet at most
  once";
- a **counterexample**: the exact sequence that breaks one invariant;
- the **plausible wrong fix**: what a competent engineer would do first, and why that's not
  enough.

The scripts beside it:
- `repro/repro.sh` (exits 1 on the world, 0 once fixed);
- `world_fix/fix.sh` (the real fix);
- for the hardness checks: `world_fix/wrong_fix.sh`, `repro/symptom_check.sh`
  (the quick check an agent would write first) and an optional `repro/dependent_check.sh`.

**Base checks (always enforced):** each runs in a fresh container at the evaluation's limits
(1 CPU / 2 GB):
- the image builds;
- `repro.sh` fails twice on the world;
- `fix.sh && repro.sh` passes twice.

**Hardness checks** (recorded in `warn` mode, a gate in `enforce`):

| Check | Why it matters |
|---|---|
| `symptom_visible` / `symptom_gone_with_fix` | the visible symptom shows on the world and the real fix removes it |
| `fix_not_local` | the real fix touches 2+ files (or a dependent breaks): a one-line patch must not be enough |
| `wrong_fix_passes_symptom` | **the wrong fix is tempting**: it makes the agent's own quick check pass… |
| `wrong_fix_fails_repro` | …but the deeper schedule still breaks it |
| `dependent_breaks_with_wrong_fix` | the local patch breaks something else that depends on it |

**The plausible wrong fix is the core of the difficulty design.** It reaches the task as
`solution/wrong_fix.sh`, and the `wrong_fix` validate gate requires the tests to score it 0.
So every accepted task has tests that tell the obvious fix apart from the real one. You can
see this in the examples below: the agents that fail usually landed on something close to
the world's wrong fix.

---

## 3. Task authoring: the instruction, the tests and the leak review

A second agent writes the task from the world (`generator/author_task.py:run_create_job`,
prompt `generator/prompts/task_author.md`):
- a short instruction in a user's voice;
- tests, one per invariant, over at least 10 seeded random schedules with kills and
  restarts, plus the exact counterexample and regression cases;
- the solution and the wrong fix.

Before it may finish, `sde-task check` and `sde-task run --as all` must pass: oracle reward 1
twice, nop reward 0, wrong fix reward 0.

**The semantic leak check** (`validator/gates.py:gate_leak`) makes sure the instruction
doesn't do the agent's thinking for it. It has two parts.

1. **The semantic reviewer** sees everything: the hidden fix, the counterexample, the world,
   the solution and the instruction.
   - It reads the instruction sentence by sentence: does this sentence, paraphrased, say
     what the fix does, where it goes, which mechanism to use, or which sequence breaks the
     system?
   - Synonyms count ("reuse the prior durable result" = idempotency on the request id).
   - Its quotes must appear in the instruction verbatim, so it can't fail a task on its own
     words.
   - One `direct` finding, or two `strong_hint`s, is a leak.
2. **The blind check:** one call sees ONLY the instruction and predicts the fix and the
   failing sequence. A judge compares that prediction with the hidden fix and answers
   `specific` | `generic` | `none`. Only `specific` (the right mechanism AND where it goes,
   or the real sequence) is a leak: a remedy any engineer would list doesn't count.

The leak review is the gate that rejects the most tasks. Two common cases:
- the symptom restates the counterexample: aws-w2-l3-t02's "after an earlier SETTLE
  changes which account has the earliest pending post, SETTLE_ALL still emits accounts in
  their original insertion order" was a `direct` leak;
- the stated rules are precise enough that a blind reader can rebuild the algorithm.

The prompt rules added tonight push the other way:
- describe what a command must report, not the checks it applies;
- for a speedup, "the same output as now", never the tie-breaks it must keep.

Every validate gate then runs again on the pipeline's own machines (§1). Gates the author
already passed are adopted when their stamp matches exactly.

---

## 4. Agent runs: being in band isn't enough

The Executor runs 5 agent runs per version. INVALID runs (timeouts, infrastructure errors)
are never scored: their slot is retried.

**Then the Trace Parser reads every trace**, even when the pass count is already in band:

1. **Failure review**, one glm-5.1 call per failed run:
   - it cites evidence ids (`E1…`) from the trace and the verifier output;
   - it attributes the failure to the **model** or the **task**;
   - it compares the failing tests with what the instruction actually asked for.
2. **Decide** (`evaluation/calibration.py`):
   - **accept** only if 1–3 of 5 pass AND no failed run is blamed on the task. A 3/5
     version with one task-blamed failure isn't accepted; it goes to
     **`repair_ambiguity`**. Example: aws-w2-l1-t01 v1 was 3/5, but one correct fix failed
     because the verifier ran on state the agent's own testing had left behind;
   - **too easy (4–5/5)** → a **pass review** over the fastest passes (why it was easy,
     the cues, the simple strategy that worked). Then `choose_mutations` picks 1–2
     operators aimed at what passed, and the **world** is mutated into v+1;
   - **too hard (0/5)** → `revise` origin **easier** on the same world (narrow the
     diagnosis, point to more evidence);
   - out of band after 8 versions → REJECTED exhausted.

**The mutation operators** (deterministic rules over the pass review and the traces;
WORLD_CONSTRUCTION.md §8):

| Rule | Fires on | Operators |
|---|---|---|
| `cues_found` | the instruction or code pointed at the fix | `remove_cues` (always first) |
| `local_reorder_or_serialize` | a local lock or reorder passed | `add_concurrent_execution`, `compose_failure_modes` |
| `self_checking_agent` | the agents fuzzed their own fix | `introduce_global_invariant`, `generate_counterexample` |
| `one_file_patch` | passing runs edited ≤1 file | `extend_cross_component_dependency` |
| `process_local_guard` | the fix kept a guard in one process | `add_persistent_state`, `inject_partial_failure` |
| `pass_review_suggestion` | the review's suggested technique | the mapped operator |

---

## 5. Six accepted tasks: how each is hard, and why agents fail

### 5.1 `sqlite-duty-leave-write-skew`: the fix that holds on one path, not under concurrency

[aws-iter1-t04](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-iter1-t04)
· database_consistency / write_skew / Python + SQLite · accepted at **v4, 1/5** (✗✗✓✗✗)
· $20.83

**The task.** "Our duty leave service sometimes accepts plans whose coverage report later
shows a day below its policy minimum…"
- for every day a pending or approved plan covers, `available_staff` must be the true
  remaining on-duty staffing and at least `minimum_staff`;
- any feasible leave window must still be recorded;
- this must hold under concurrent use and across kills and restarts;
- the existing retry, approval and validation behavior must keep working.

**How it's hard.** This is the revision pipeline's longest path:

| Version | Origin | Passes | What happened |
|---|---|---|---|
| v1 | initial | 0/5 | too hard |
| v2 | easier | 5/5 | localized the diagnosis; too easy |
| v3 | world mutation | 5/5 | `remove_cues`, `introduce_global_invariant`, `generate_counterexample`; still too easy, because a local lock passed |
| v4 | world mutation | **1/5** | `add_concurrent_execution`, `compose_failure_modes`; accepted |

The world's **plausible wrong fix**: "check per-day distinct active-staff leave overlap at
submission and approval, and report active staff minus unavailable". It ignores duty
assignments, so roster changes can still over-commit coverage. After v4's mutation, a
roster unassignment (the counterexample's "a roster actor unassigns Dan") and concurrent
approvals interact.

**Why agents fail** (4 of 5 runs, all attributed to the model):
- [run 1](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-iter1-t04-v4-rollout1-r0):
  four tests failed: "planned coverage 1 < minimum", "concurrent operations over-committed
  assigned coverage", and "infeasible roster unassignment was allowed". The agent fixed
  leave approvals but **let a roster change break the daily minimum it was told to keep**;
- [run 5](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-iter1-t04-v4-rollout5-r0):
  "the agent's own validation runs crashed with unhandled AssertionErrors, yet the agent
  declared the task complete anyway".

**Pattern:** the agent fixed the bug on the path it reproduced (submit and approve), and
missed the second writer (roster changes) and the concurrent schedule.

### 5.2 `bounded-ledger-reconciliation`: correct semantics, broken resource envelope

[aws-agent-b1-t09](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-agent-b1-t09)
· performance_resources / memory_bound / Java · accepted at **v4, 3/5** (✓✗✓✓✗) · $14.10

**The task.** A six-million-event ledger must reconcile:
- within 75 s, under a 128 MiB Java heap;
- with output that matches an independent reference exactly: per-account counts, totals,
  first/last bounds, and a checksum over each account's events in timestamp order;
- treating account ids that differ only by zero padding as one account.

**How it's hard.**

| Version | Origin | Passes | What happened |
|---|---|---|---|
| v1 | initial | 5/5 | "stream it" was enough |
| v2 | world mutation | 4/5 | `introduce_global_invariant`: the checksum is defined in timestamp order, not stream order, so plain streaming is wrong |
| v3 | world mutation | 0/5 | `extend_cross_component_dependency`: the memory retention moved out of the reader into a shared cache; overshot |
| v4 | easier | **3/5** | accepted |

The **plausible wrong fix**: "rewrite Reconciler.java into a bounded external-sort
reconciler while leaving Main.java untouched". The memory failure goes away, but equivalent
account ids are split and the reference comparison fails.

**Why agents fail:**
- [run 2](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-agent-b1-t09-v4-rollout2-r0)
  (205 steps): `OutOfMemoryError` in `ArrayList.grow` in the agent's *own* new code. It
  kept the semantics and **broke the 128 MiB bound** the instruction stated;
- [run 5](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-agent-b1-t09-v4-rollout5-r0)
  (9 steps): "the automated string replacement did not match the actual method, so
  compilation still fails and the runtime remains the stale old implementation". The agent
  said so in its final message and finished anyway.

The passing runs differed: run 4 wrote its own `reference.py` and `fuzz.py` and compared
outputs before finishing.

**Pattern:** two constraints that pull against each other (exact semantics and bounded
memory). Agents satisfy one and break the other.

### 5.3 `stateful-template-snapshot-edit`: fixing snapshots breaks edits

[aws-w1-l4-t05](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-w1-l4-t05)
· compilers_parsers / stateful_incremental_parse / Go · accepted at **v2, 3/5** (✓✓✗✓✗)
· $5.07

**The task.** "Users edit a template with `edit` and capture its current definition with
`snap`."
- after an edit, rendering must use the edited expression;
- a snapshot must keep the definition captured at `snap` time;
- `set`, `tmpl`, `edit`, `snap` and `render` must keep working.

**How it's hard.** v1 went 4/5: copying the captured template on `snap` was enough. The v2
mutation (`extend_cross_component_dependency`, `generate_counterexample`) moved the
sharing out of `snap` itself:
- `tmpl` now interns templates by source text, so two names with the same source share
  one object;
- edits mutate shared part objects in place.

The world's **plausible wrong fix** is "give the captured name a fresh template with a
copied parts slice". The copied slice still points at part objects a later edit updates in
place, so the snapshot follows the edit.

**Why agents fail:**
- [run 3](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-w1-l4-t05-v2-rollout3-r0):
  the agent deep-copied snapshot captures and cloned templates "when sharing cached parsed
  source". Snapshots were fixed, but **edits stopped reaching the template**. The test
  expected `answer=7 - -2` and the output kept the old `answer=7 * -2 + 3`, and the exact
  counterexample printed `1 1` instead of `2 1`. It verified only with `go test ./... &&
  go vet ./...`;
- [run 5](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-w1-l4-t05-v2-rollout5-r0):
  the same regression. The agent's own manual run already showed `render a` printing the
  pre-edit value.

**Pattern:** this is the classic one. **The bug gets fixed, and in doing so the agent
breaks an invariant the instruction said to keep** (edits must show in renders). The two
requirements are coupled through shared state, so the obvious isolation fix breaks the
other half.

### 5.4 `greedy-allocator-affinity-conflict`: a correct algorithm the verifier couldn't start

[aws-w2-l5-t01](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-w2-l5-t01)
· algorithmic_constraints / global_constraint_interaction / Go · accepted at **v1, 3/5**
(✓✓✗✗✓) · $1.00

**The task.** `POST /allocate` sometimes returns `success=false` for a feasible request.
- every feasible request must succeed, with assignments that respect capacities,
  conflicts and affinity groups;
- repeat and concurrent requests, including across a kill and restart, get the same
  result;
- invalid or infeasible requests are still rejected.

**How it's hard.** The defect is a greedy allocator that commits affinity groups too
early. The **plausible wrong fix** relocates the most recently placed group when a task
won't fit. That solves the single-group counterexample, but fails when *two* groups must
move together. The world spells out an instance (machines A(12)/B(10), groups web and db,
three size-6 tasks with conflicts) where single-group relocation still rejects a feasible
request. So the tests require a real search, not a better heuristic.

**Why agents fail:**
- [run 3](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-w2-l5-t01-v1-rollout3-r0):
  its own suite printed "ALL API TESTS PASSED" and "RESTART RESPONSE IDENTICAL". But the
  verifier's service "exited during startup": the agent's **leftover background allocator
  was still holding port :8080**, and it never stopped it before finishing;
- [run 4](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-w2-l5-t01-v1-rollout4-r0):
  the same startup failure.

The passing runs ended with the environment clean.

**Pattern:** the algorithm was right, but **the agent left the system in a state the
verifier couldn't run in**. The task asks for a service that survives restarts, and
leaving a stale process on its port breaks exactly that.

### 5.5 `replicated-log-failover-dedup`: broad edits break the exact semantics

[aws-iter2-t10](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-iter2-t10)
· distributed_concurrency / adversarial_schedule / Go · accepted at **v1, 2/5** (✓✗✗✓✗)
· $4.38

**The task.** After failover, a replicated log returns `ok` for a request whose copy on the
responding node is uncommitted, and nodes keep uncommitted copies of requests another node
committed.
- a successful `propose` must mean a committed entry on that node;
- once any node commits a request id, every node that has it must commit it;
- the schedule command's JSON (pending, redirect, errors, roles, terms, balances, logs,
  dedup state, idempotent retries) must keep working.

**How it's hard.** The **plausible wrong fix**: clear dedup for truncated entries and only
return `ok` for committed entries. That removes the stale `ok`s but never upgrades follower
entries when the leader commits and replicates. The counterexample is a 9-step schedule:
propose, replicate twice, commit, replicate twice, crash the leader, promote n2, propose
the same id again.

**Why agents fail** (3 of 5):
- [run 3](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-iter2-t10-v1-rollout3-r0)
  (14 steps):
  - it **removed the `term <= n.term` guard in `Promote` and replaced the `ErrConflict`
    rejection** with conflict-suffix replacement;
  - those broad changes broke the exact outputs the regression tests assert for roles,
    retries, errors and state;
  - its final message names a panic it had just introduced and "only states a plan to fix
    it";
- [run 2](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-iter2-t10-v1-rollout2-r0)
  (36 steps): its `ReplicateLog` marked entries committed too early, so later commits
  skipped applying the amount. The agent diagnosed this itself but never ran the fix.

The passing runs took 59 and 77 steps; the failing runs took 14–40.

**Pattern:** **the agent fixed the stated anomaly by loosening protocol rules** that the
instruction said must keep working. The short runs that stop early fail.

### 5.6 `charge-ledger-retry-credit`: the guarded fix on the wrong path, plus regressions

[aws-iter2-t08](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-iter2-t08)
· database_consistency / isolation_anomaly / Python + SQLite · accepted at **v2, 2/5**
(✓✗✗✗✓) · $4.84

**The task.** "The charge ledger service sometimes reports a wallet balance of 20 for a
single 10 charge after a worker is killed and restarted."
- a charge credits the wallet at most once;
- every balance equals the sum of that customer's applied ledger entries;
- `retry` is safe after crashes and concurrent workers.

**How it's hard.** v1 went 4/5: one guarded transaction in `apply_charge` fixed every entry
point, because `retry` settled through `apply_charge`. The v2 mutation
(`extend_cross_component_dependency`, `add_concurrent_execution`) moved the crash-recovery
credit onto a path that doesn't go through `apply_charge`: `Store.settle_pending` credits
wallets directly.

The world's **plausible wrong fix** is now exactly v1's winning fix: "make apply_charge one
atomic, guarded transaction… but recovery still settles pending work through the store
path".

**Why agents fail** (3 of 5): each failed `test_regression_commands_and_edge_cases`.
- [run 3](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-iter2-t08-v2-rollout3-r0):
  it made "ad-hoc edits to `store.py` (rewriting `pending_charges` and adding a
  `credit_wallet` helper)". It verified only with its own scripts, "declared the task
  complete twice", and said all verification passed, but **the regression cases broke**:
  submit/apply idempotency, and a customer with no charges reporting balance 0;
- [run 2](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-iter2-t08-v2-rollout2-r0)
  and
  [run 4](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/traces/aws-iter2-t08-v2-rollout4-r0):
  the same test.

**Pattern:** the mutation turned v1's winning fix into the world's wrong fix. Agents that
look past it rewrite the store and **break the must-keep-working behavior** along the way.

---

## 6. Patterns across the accepted tasks

| Pattern | Tasks | What the pipeline does to cause it |
|---|---|---|
| **Fix the bug, break a stated invariant** (the instruction said keep X working) | 5.3, 5.5, 5.6 | tests always include the regression and edge cases the instruction names; two requirements coupled through shared state, so isolating one breaks the other |
| **Fix the path you reproduced, miss the other writer** | 5.1, 5.6 | `extend_cross_component_dependency` / `add_concurrent_execution` mutations move the defect onto a second path; tests run seeded concurrent schedules with kills |
| **Satisfy one constraint, break the other** (semantics vs resources) | 5.2 | `introduce_global_invariant` adds a rule (timestamp-order checksums) that the obvious optimization (streaming) violates |
| **Land on the plausible wrong fix** | 5.3, 5.4, 5.6 | the world's `plausible_wrong_fix` becomes `wrong_fix.sh`, and the `wrong_fix` gate requires the tests to score it 0 |
| **Declare done despite your own failing evidence** | 5.1 (run 5), 5.2 (run 5), 5.5 (runs 2–3), 5.6 (run 3) | nothing beyond hidden tests that are thorough: the instruction states every outcome, so a run that stops at "my script passed" fails on cases it never ran |
| **Leave the environment dirty** | 5.4 | restart and concurrency requirements mean the verifier starts its own service; a stale process on the port fails it |

In the traces, the agents that pass do three things:
- they build a reference or differential check (5.2: `reference.py`, `fuzz.py`);
- they take longer (5.5: 59–77 steps vs 14–40);
- they finish with the system clean.

The agents that fail stop at the first fix that makes their own reproduction pass.

## 7. Reusing worlds: same-world tasks, the picker bug and reseeding

Since 2026-11-18 (window 2) no new worlds are built: new tasks start at task authoring on a
world that already passed `world_check` (`POST /v1/batches` with `"reuse_worlds": true`).
The code:
- `api/submit.py:world_pool` / `pick_worlds` picks the world, using the policy in
  `shared/taxonomy/world_selection.py`;
- the first job is a `create` with `input.reused = true`;
- the task's slot records `reused_from` (the task that built the world) and
  `reused_world` (its sha).

Reuse is cheap: a world costs 20–45 minutes of authoring, and a reused task skips it. But
it brought three problems to light.

### 7.1 The picker handed out a world whose own task was still running

Before the fix, `world_pool` counted only earlier *reuses* of a world, not the live task
that built it. So a new task could start on a world while that world's first task was still
being authored or evaluated. Two examples:

- [aws-w3-l3-t01](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-w3-l3-t01)
  (submitted 22:25 UTC) got world
  [`7d8725b8`](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/worlds/7d8725b856889c8df52a433d0876694be7899b521512902bc71e897e187a71c0)
  from [aws-iter2-t09](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-iter2-t09).
  At that moment iter2-t09 was still CALIBRATING (v2 in agent runs).
- [aws-w2-l3-t01](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-w2-l3-t01)
  and [aws-w2-l3-t03](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-w2-l3-t03),
  submitted in the same batch at the same second (21:40:18 UTC), both got worlds from
  [aws-agent-b1-t04](https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/aws-agent-b1-t04).
  These were two worlds in that task's lineage
  ([`6d9d452d`](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/worlds/6d9d452d6c960c58a283e8f45a0d6cf0469ed6b5ef52ee816f2e490dbbd795b3)
  and [`700cb4c7`](https://librarian-anyone-zealand-lee.trycloudflare.com/artifacts/worlds/700cb4c727dd235834d42bbf6c362d1da89b3a7a346796b84226aa5bd2c4a0d0)).
  Both became `durable-quorum-ledger`, and both went **5/5** at v1: a near-duplicate pair.

**Fixes:**
- `5539cec` (lane-l1-live-skip, deployed ONLY=api about 22:40 UTC): never reuse a world
  whose own task is live;
- `ed108aa` then relaxed this to "share a world with its live task, up to 2 tasks, with
  repeats last" (the user's reuse-only rule).

### 7.2 Same world, same task: a reworded request

A world fixes the task more than it seems. task_author.md requires **one dedicated test
per world invariant**, and most worlds have only 2–3 invariants and one counterexample. So
every task written on a given world tests the same invariants with the same counterexample.

`lane-l3-reuse-distinct` (live from release 35616118, about 21:55 UTC) shows the author the
earlier tasks' instructions on that world, and asks for "another of the world's guarantees
or invariants, another entry point or user-visible symptom; never the same request
reworded" (`shared/jobs/worker.py:earlier_instructions`,
`generator/author_task.py:earlier_tasks_section`).

**Measured on aws-w3-l3-t01:**
- the author did see aws-iter2-t09's instruction;
- the world (`7d8725b8`) has 2 invariants: a winning-observation tie-break and a sort order;
- the new instruction ("Capacity auditors run `/app/reconcile`… must remain the same audit
  report the command produces today… exceed their 256 MB / 15-second budget…") has the
  **same symptom, the same guarantees and the same entry point** as iter2-t09's. It's a
  reworded request.

It is a *harder* rewording: the speedup rule (`lane-l3-same-output`) removed the stated
tie-break and sort rules, so the agent must find them in the code. The leak review was
clean after 2 repairs; the blind judge called its prediction generic.

**The fix: reseed + replace (the user's call).** A reuse no longer writes a task straight
onto the picked world.

1. **Reseed.** Sample a *new* spec from `shared/taxonomy/taxonomy.py`:
   - the same family and environment;
   - a mechanism not yet used on that world, never `greedy_counterexample`;
   - a new shape and dial.
2. **Replace.** A variant mutation (`world_variant.md`) **fixes the source world's bug**
   and plants **one defect of the reseeded mechanism**. That gives the world new
   invariants, a new counterexample, a new fix and a new plausible wrong fix. It keeps the
   codebase and the domain, which are the expensive parts of a world. The variant goes
   through `world_check` like any world.
3. **Create.** The task is written on the variant, and the author still sees the earlier
   tasks' instructions on the source world (reuse-distinct).

This removes the structural cause above: the new task has its own invariants to test, so
it can't be the old request reworded. The one-test-per-invariant rule stays. It ships
with L1's `lane-l1-variant` and L4's `world_variant.md` in the next API + generation
deploy.

### 7.3 Same domain everywhere: "ledger"

The world authors default to a ledger when nothing names a domain. Every named task in
lane L3 tonight was one:
- `replicated-ledger-recovery` (twice, on different worlds);
- `ledger-reconciler`, `replicated-ledger-failover`, `durable-quorum-ledger` (twice);
- `allocation-ledger-canonical-export`.

Across all lanes, 13 of 46 CALIBRATING tasks had "ledger" in the name. Among the 15
accepted production tasks, 7 are ledgers, charges or bookings (e.g.
`bounded-ledger-reconciliation`, `charge-ledger-retry-credit`,
`postgres-booking-overcommit`).

The duplicate check on world selection keyed on `taxonomy.domain`, which is the family
label, so it couldn't see this. `5539cec` now counts bank and account names as ledger, and
`4505796` adds a per-window domain cap. Under reuse-only, new worlds can't add new domains,
so world selection is the only lever.

## 8. Caveats

- **Sample:** these six are 6 of the 15 production tasks accepted so far (the `lt-*` and
  `prodmock-*` accepted tasks are load-test mocks). Five of the six families are
  represented here; cross_component_stateful has two accepted tasks not covered
  (aws-iter2-t01, aws-iter2-t07). Database tasks are 6 of the 15 accepted.
- **Hardness checks are in `warn` mode:** they're recorded, not enforced. The
  plausible-wrong-fix *gate* that is enforced is validate's `wrong_fix`.
- **Costs** are the pipeline's ledger, which prices prompt tokens at the uncached rate
  (about 2–3× the gateway's own figure on long authoring sessions; see
  docs/ITERATION_LOG.md).
- **Links:** the dashboard links depend on the current tunnel URL (top of this doc).

See also: [VALIDATION_GATES.md](VALIDATION_GATES.md), [EVALUATION.md](EVALUATION.md),
[WORLD_CONSTRUCTION.md](WORLD_CONSTRUCTION.md),
[TASK_GENERATION_RULES.md](TASK_GENERATION_RULES.md).
