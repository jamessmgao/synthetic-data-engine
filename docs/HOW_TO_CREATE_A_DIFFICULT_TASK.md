# How we create a difficult task

This is the end-to-end recipe: what "difficult" means here, how the pipeline builds a task, the
design rules that make it hard, the ways tasks turn out too easy (with the evidence from the runs of
2026-11-18), and a checklist for anyone tuning the generator. It ties together the deeper docs:
[TASK_DIFFICULTY.md](TASK_DIFFICULTY.md) (theory), [WORLD_CONSTRUCTION.md](WORLD_CONSTRUCTION.md)
(worlds), [VALIDATION_AND_FAILURE_MODES.md](VALIDATION_AND_FAILURE_MODES.md) (checks and why agents
fail), [HARD_TASK_GENERATION_GUIDE.md](HARD_TASK_GENERATION_GUIDE.md) (families) and
[HILLCLIMBING_ALGORITHM.md](HILLCLIMBING_ALGORITHM.md) (how we tune it).

## 1. What "difficult" means

A task is **accepted** when the pinned solver (terminus-2 + glm-5.3-flash, 1 h agent timeout)
passes it **1 to 3 times out of 5** agent runs (`N_RUNS=5`, `BAND_LOW=1`, `BAND_HIGH=3` in
shared/contracts/config.py). Our objective is **yield**: the share of finished tasks that land in that band.

- **5/5 or 4/5 is too easy**: the task gets revised harder (a world mutation).
- **0/5 is too hard**: revised easier, or repaired if the Trace Parser blames the task.
- **In band, but for the wrong reason** doesn't count. Runs that are INVALID (infra), cancelled or
  skipped aren't failures, and a timeout is only a failure if the Trace Parser rules it the model's
  fault (it wedged its terminal, looped or gave up).

"Difficult" therefore means: **a capable agent can solve it, but often doesn't**, because finding or
correctly fixing the problem takes real investigation, not because the task is broken or vague.

## 2. How a task is built (the pipeline)

```
world → world_check → create (task authoring) → validate → 5 agent runs → analysis → decide
                                                                                     ├─ accept (1-3/5)
                                                                                     ├─ too easy → mutate the world → new version
                                                                                     ├─ too hard → easier revision
                                                                                     └─ task blamed → repair
```

| Stage | Who | What it produces | What can stop it |
|---|---|---|---|
| **world** | an authoring agent (terminus-2 + GLM-5.1, in a sandbox) | a buggy *system*: `environment/`, `world_fix/fix.sh`, `world_fix/wrong_fix.sh`, `repro/repro.sh`, `repro/symptom_check.sh`, `world.json` (spec, invariants, counterexample, plausible wrong fix) | its own self-check |
| **world_check** | deterministic, no model | proof the bug is real and fixable | see §4.1 |
| **create** | an authoring agent | the *task*: `instruction.md` (what the user sees), hidden `tests/`, `solution/`, `task.toml` | the leak review (§4.3) |
| **validate** | gates | structural, lint, oracle ×2, nop, wrong-fix, leak | see §4.2 |
| **agent runs** | the pinned solver, 5 times | pass / fail per run, traces | — |
| **analysis** | the Trace Parser | a failure explanation per failed run, a pass review for too-easy versions, the decision | — |

A task can go through up to 8 versions (`SDE_MAX_VERSIONS`). Two newer mechanics shorten the loop:
**early stopping** (decide as soon as the band verdict is certain, e.g. 1 pass + 2 fails with 2 runs
left) and **timeout review** (the Trace Parser rules each timed-out run model-fault or infra).

### What a submission specifies

`POST /v1/batches` with `area` (families), optionally `dial` (1-3, 3 = hardest), `count`. Each task
samples a spec: **family**, **mechanism** (the specific defect pattern), **domain** (the kind of
system, about 40 kinds: a hospital bed allocator, a CRDT editor, a DNS cache…), shape, environment,
verification method. The family brief (generator/families.py) and the mechanism's note steer the
world agent; the domain makes the system realistic and keeps tasks diverse.

## 3. The design rules that make a task hard

These come from the six accepted tasks studied in VALIDATION_AND_FAILURE_MODES.md §5-6 and from the
pass reviews of the too-easy ones.

### 3.1 The bug lives in a real system, not a kernel
The defect's data must flow through several modules (parsing, persistence, a report, an incremental
path). A 2-5 file, 4 KB program is read in one `cat` and diagnosed at step 3-4: that was the single
most common reason for 5/5 tonight.

### 3.2 The fix is not local, and the obvious fix is wrong
- **`fix_not_local`**: the real fix changes 2+ files, or a one-file patch breaks a dependent elsewhere
  (`dependent_check.sh`).
- **A plausible wrong fix exists and is implemented** (`wrong_fix.sh`). It must pass the agent's
  first reproduction (`symptom_check.sh`) but still fail the real invariant (`repro.sh`), so an agent
  that stops at "my repro passes" fails the hidden tests. The validate gate requires the tests to
  score the wrong fix 0.

### 3.3 The instruction states outcomes, never mechanisms
The instruction describes the symptom users saw and the guarantee that must hold, through the
system's interface (commands, inputs, outputs, files). It never names the fix's mechanism or
location, the failing sequence, crash hooks or stage names, and never singles out the exact cases the
defect breaks. Short, user-voiced instructions (75-130 words) worked best.

**The honest guarantee must not name the fix.** If the only true way to state the requirement is
"the result is optimal" and the bug is "a greedy rule", the guarantee *is* the fix. That's why the
greedy-vs-optimal mechanisms were retired: 0 accepted of 11 tasks.

### 3.4 Never ship the answer
The environment must contain no correct or reference version of the logic: no `--reference` /
`--audit` mode, no `plan_reference()`, no exact fallback behind a flag. Tonight one too-easy task's
entire fix was `return plan_reference(jobs, plan);`. Brute-force references belong in the world
check and the hidden tests only.

### 3.5 The defect hides from random testing
Agents that pass build a reference or a fuzzer and diff against it. A defect that any small random
input exposes is found in minutes. Make it depend on a **structure or history**: an edit sequence,
ties across two stages, a solution straddling a boundary, a rule that fires late, a crash at one
point, an interleaving. The counterexample in `world.json` has that structure (it is not what a
random generator would hit).

### 3.6 Coupled requirements
The hardest accepted tasks couple two requirements through shared state, so fixing one breaks the
other (fix the bug *and* keep a stated invariant; satisfy the constraint *and* the resource limit).
The tests always include the regression and the edge cases the instruction names.

### 3.7 Thorough hidden tests
The tests check every outcome the instruction states, including restarts, concurrency and the
dependent, so an agent that stops at its own passing script fails on cases it never ran. Tests start
their own services (a stale process left by the agent fails them) and must finish well inside the
verifier's 900 s.

## 4. The gates (what keeps tasks honest)

### 4.1 world_check (shared/taskformat/world_check.py)
`structure` → `build` → `repro_fails` (twice: the bug is real and stable) → `repro_passes_with_fix`
(twice: it's fixable), then the hardness checks: `symptom_visible`, `symptom_gone_with_fix`,
`fix_not_local`, `wrong_fix_survives`, and a warning `limit_named` (a function whose name or comment
states its own limit gives the bug away).

### 4.2 validate (validator/gates.py)
structural, lint, **oracle ×2** (the reference solution passes, twice, with the same per-test
results), **nop** (doing nothing scores 0), **wrong_fix** (the plausible wrong fix scores 0), and
**leak**.

### 4.3 The leak review
Two independent checks on the instruction:
- **semantic review**: a model compares the instruction with the hidden notes, the world and the
  solution, and quotes sentences that give the fix or the failing sequence away (a leak is any
  "direct" finding or 2+ "strong hints"; a quote must really appear in the instruction to count);
- **blind check**: a model that sees *only* the instruction predicts the fix, and a judge compares the
  prediction with the real fix: only a **specific** match (mechanism *and* location) is a leak.

A failed review sends the draft back for a repair, up to 2 per version. Most rejections tonight were
blind-check failures where the guarantee named a textbook remedy (an idempotency key, an atomic
transition, an index).

## 5. Why tasks come out too easy (evidence, 2026-11-18)

From the Trace Parser's pass reviews of every too-easy version:

| Cause | Example | Rule it breaks |
|---|---|---|
| Tiny codebase, read in one command, diagnosed at step 3-4 | most algorithmic tasks (2-5 files, ~4 KB) | §3.1 |
| The correct algorithm shipped in the environment | aws-w2-l2-t03: fix = `return plan_reference(…)`; also b1-t03, b1-t08, w2-l2-t01 | §3.4 |
| The guarantee names the fix ("optimal" vs a greedy rule) | greedy_counterexample 0/5 accepted, optimality_required 0/6 | §3.3 |
| The instruction names the textbook problem | "total weight as high as possible" → "weighted interval scheduling" before reading code | §3.3 |
| Fail-loud bugs (a crash on first use) | aws-iter2-t11 v1: 5/5 | §3.5 (a crash is its own counterexample) |
| One index fixes a performance task | performance tasks never landed in band | §3.2 (the obvious optimization must break an exact result) |

And why some come out **too hard** or **invalid**: an unclosed heredoc wedging the agent's terminal,
tests that run past the 900 s verifier limit (aws-w2-l1-t02), and the agent closing its own terminal
with `exit` (now scored by the verifier instead of re-run).

## 6. Per-family notes

Each family's brief in generator/families.py states its lever (where the difficulty comes from), the
kind of world, the counterexample, the scaffold and the dependent. In short:

- **algorithmic_constraints**: an exact algorithm (DP, branch-and-bound, flow, matching) right on its
  textbook core, with a subtle defect inside it that needs cross-part reasoning. The 11 current
  mechanisms include unsound dominance pruning, residual flow not cancelled on a capacity drop, a
  local tie-break that isn't the global lexicographic order, a state key missing a rare component,
  a sweep that loses solutions crossing the cycle boundary, rollback with path compression, stale
  incremental DP, an inadmissible bound with group costs, and capacity shared across stages.
- **distributed_concurrency / database_consistency / cross_component_stateful**: the failure needs
  an interleaving, a retry or a crash between a commit and its side effect; the DBA's first fix (a
  lock, a conditional update, a unique constraint) is the wrong fix, defeated by a second table, a
  retry or a crash.
- **performance_resources**: the obvious optimization silently changes an exact result (an order
  fixed by a later event, a two-key tie-break), so the hard part is keeping the semantics.
- **compilers_parsers**: semantics-changing transforms and fragmented input where the local rule is
  right and the global one isn't.

## 7. Checklist for a new mechanism, brief or prompt change

Before proposing it, check that the resulting tasks would have:
1. [ ] a system of several modules the defect's data flows through (§3.1);
2. [ ] no reference / correct / exact fallback in the environment (§3.4);
3. [ ] a defect that needs a specific structure or history, not a random input (§3.5);
4. [ ] a real fix that isn't one local patch, and a plausible wrong fix that passes the agent's first
       repro but fails the invariant (§3.2);
5. [ ] an honest guarantee that doesn't name the fix or the textbook problem (§3.3);
6. [ ] coupled requirements the tests check together, plus every stated edge case (§3.6-3.7);
7. [ ] tests that finish well inside 900 s and start their own services;
8. [ ] nothing that makes it fail loud on first use.

Then measure it (HILLCLIMBING_ALGORITHM.md): one change per lever per window, judged on the tasks
authored after it, by pass-count histogram, yield, and the pass reviews' "steps to diagnosis".

## 8. Running it

```bash
# A batch of new hard algorithmic worlds (the API key is read from SSM; never print it)
POST /v1/batches {"batch_id": "aws-wN-<lane>", "count": 5, "area": ["algorithmic_constraints"], "dial": 3}

# Watch: GET /v1/tasks/<id> (versions, leak reviews, agent runs with the Trace Parser's
# explanations), the dashboard, and traces in S3 traces/<job>/.
```

New tasks start from a new world (the user's rule since 2026-11-19 00:00); `reuse_worlds` batches
(reseeded variants of earlier worlds) exist but are off for now.
