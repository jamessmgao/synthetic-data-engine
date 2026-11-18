# Validation gates, as built

Two gate jobs decide whether authored work may go on: `world_check` for a world (agent
mode) and `validate` for a task bundle (both modes). Both are deterministic except the
leak review, hold a Docker slot, record their result for adoption on retry, and on failure
return `feedback`: the exact evidence a repair round sends back to the author.

## The task gates: `validator/gates.py:run_validate_job`

In order, cheapest first, stopping at the first failure:

| gate | function | passes when |
|---|---|---|
| `structural` | `gate_structural` → `validator/validate.py:validate_task` | the bundle's layout and `task.toml` schema are valid (errors block, warnings are recorded) |
| `lint` | `gate_lint` | `harbor lint --json --select ALL` has no finding in `config.LINT_BLOCKING_RULES` (the rest are warnings) |
| `oracle1`, `oracle2` | `gate_oracle` | the reference solution scores reward 1, twice (a flaky test fails one of them) |
| `nop` | `gate_nop` | the untouched environment scores reward exactly 0 with a real verdict (the tests detect the bug) |
| `wrong_fix` | `gate_wrong_fix` | `solution/wrong_fix.sh`, run like the oracle, scores reward exactly 0 (the tests reject the plausible wrong fix); skipped only if the bundle has none, which agent-mode drafts can't |
| `leak` | `gate_leak` | the instruction gives away neither the fix nor the failing sequence (below) |

Each Harbor step runs as `harbor run --job-name <step> -o <trace_dir>`; a finished step is
adopted, a half-finished one renamed `<step>.abandoned-<n>`.

### The leak gate: semantic review + blind prediction

`validator/gates.py:gate_leak`, also run by the create job (`generator/author_task.py:run_create_job`)
so validate adopts its record. Skipped when `SDE_LEAK_REVIEW=0` or the bundle has no hidden spec.

**What the reviewer sees** (call 1, `LEAK_SYSTEM`): the bundle's hidden notes (root cause,
subgoals, correctness constraints, from its `README.md`), the world the task was written
from (`_world_context`: invariants, the counterexample sequence and what was observed, the
plausible wrong fix, `world_fix/fix.sh`; found through `task.toml`'s
`generation.world_sha256` under `<work>/worlds/`), the reference solution (`_solution_context`,
`solution/` minus `wrong_fix.sh`), and the instruction.

**What it judges**: sentence by sentence, does this sentence, *paraphrased*, tell the agent
what the fix does, where it goes, which mechanism to use, or which sequence breaks the
system? Synonyms count ("reuse the prior durable result" = idempotency on the request ID).
And for the whole instruction: could an agent reproduce the counterexample or reach the fix
from it alone, without reading the code? Crash hooks and stage lists are leaks.

**Grading**: each finding has `quote`, `reveals`, `severity` (`direct` | `strong_hint`).
A quote counts only if it appears in the instruction verbatim (normalized whitespace and
markdown), so a hallucinating reviewer can't fail a task on its own words; the paraphrase is
in `reveals`. **A leak is any `direct` finding, or two or more `strong_hint`s.** An ungraded
finding counts as `direct`.

**The blind check** (calls 2 and 3, on unless `SDE_LEAK_BLIND=0`): a call that sees ONLY the
instruction (`BLIND_SYSTEM`) predicts the fix and the failing sequence; a judge call
(`JUDGE_SYSTEM`) compares the prediction with the hidden fix and counterexample and answers
`specific` | `generic` | `none`. **Only `specific`** (the real mechanism AND where it applies,
or the real sequence) **is a leak**: a generic remedy any engineer would list does not fail a
well-stated invariant.

**Record**: `<work>/leak_reviews/<bundle_sha>/review.json` (content-addressed bundles; else
`<trace_dir>/leak/`), version 2: `{review, blind: {prediction, judge}, calls: [{call,
request_sha256, cost_usd}], cost_usd}`. Adopted (cost 0) by every later job on the same
bundle; a record from before the blind check is reviewed again. The gate's detail reports
`leaks, unverified_quotes, direct, strong_hints, blind, blind_leak, had_world, calls, cost_usd`.
The feedback lists the verified sentences with what they reveal and, for a blind leak, the
prediction, and asks for an interface-level rewrite.

Tests: `tests/shared/taskformat/test_leak_review.py` (P1's real instruction, world and fix, from
`tests/shared/taskformat/fixtures/p1`).

## The world gates: `shared/taskformat/world_check.py:run_world_check_job`

### Base checks (always enforced)

| check | passes when |
|---|---|
| `structure` | `environment/Dockerfile`, `repro/repro.sh`, `world_fix/fix.sh`, `world.json` exist; world.json has invariants and a counterexample sequence; the Dockerfile uses an allowed base image and every `COPY` source exists |
| `build` | the world's image builds (base image pinned to its digest), and the check image (scripts baked in at `/world/`) builds |
| `repro_fails` | `bash /world/repro/repro.sh` exits 1, twice: the counterexample is real and deterministic |
| `repro_passes_with_fix` | `bash /world/world_fix/fix.sh && bash /world/repro/repro.sh` exits 0, twice: it is fixable |

Every script runs in a fresh container of the check image (1 CPU / 2 GB, the evaluation's
limits), from its `WORKDIR`. Exit codes other than 0 / 1 mean the script itself failed.

### Hardness checks (`SDE_WORLD_HARDNESS`)

`hardness_mode()`: **`warn`** (the default, chosen by the user to measure before enforcing):
every hardness check runs and is recorded in `result.hardness`, the world passes.
**`enforce`**: the first failing one fails the world (the others are listed), and
`repro/symptom_check.sh` and `world_fix/wrong_fix.sh` become required files. **`off`**: skipped.

`_hardness` runs them all (no early stop, so warn measures every one):

| check | passes when | why |
|---|---|---|
| `hardness_files` | the two scripts exist (only reported when missing) | |
| `symptom_visible` | `symptom_check.sh` exits 1 on the world | the visible symptom, the first reproduction an agent writes, shows |
| `symptom_gone_with_fix` | `fix.sh && symptom_check.sh` exits 0 | the real fix removes it |
| `fix_not_local` | `docker diff` after `fix.sh` in a fresh container shows 2+ system files (scratch, caches and the check's own files ignored, `_changed_files`), **or** a `repro/dependent_check.sh` passes the next three | a one-file patch must not be enough (the f1 pattern, docs/ANALYSIS.md) |
| `dependent_holds` | `dependent_check.sh` exits 0 on the world | the dependent works today |
| `dependent_holds_with_fix` | `fix.sh && dependent_check.sh` exits 0 | the real fix keeps it working |
| `dependent_breaks_with_wrong_fix` | `wrong_fix.sh && dependent_check.sh` exits 1 | the local patch breaks it |
| `wrong_fix_passes_symptom` | `wrong_fix.sh && symptom_check.sh` exits 0 | the wrong fix is tempting: it passes the agent's own quick check |
| `wrong_fix_fails_repro` | `wrong_fix.sh && repro.sh` exits 1 | and is still wrong on the deep schedule |

`result.hardness = {mode, symptom_ok, spans_or_dependent, wrong_fix_ok, failed: [...],
details: {fix_files, via: "files" | "dependent" | null, missing?}}`.

**The author sees them**: `sde-world check` (`authoring/sde_world.py:hardness_warnings`) prints
one `WARNING:` per failed check with what to change, and exits 0 in warn mode.

**The next mutation does not use them (yet)**: `choose_mutations` ignores the hardness result
(using it as a tie-break was tried and parked: docs/TODO.md, branch `park/hardness-tiebreak`).
`world_mutation.md` has a hardness section (`generator/author_task.py:hardness_section`) for a
mutation input's `hardness`, but nothing fills it, so it renders "(not recorded for the parent
world)". Today the result is for measurement (`world_check.json`) and for the author's own
WARNINGs.

Tests: `tests/shared/taskformat/test_world_check.py`.

## On failure: the repair loop

- **world_check FAILED** → `world-repair<k+1>` (type `world`, origin `repair`, with the
  feedback), while `k < MAX_REPAIRS_PER_VERSION` (2, `shared/contracts/models.py`); then REJECTED
  `invalid` (`shared/jobs/handoffs.py:after_world_check`). A `world` job that FAILED is REJECTED
  `invalid` (`after_world`).
- **validate FAILED** → `repair<k+1>` (type `revise`, origin `repair`, `feedback` = the failed
  gate's evidence, the world kept), while `k < 2`; then REJECTED `invalid` (`after_validate`).
- **create / revise FAILED with `failure_kind: author` and a bundle** (the leak review or the
  self-check at create time) → the same `repair<k+1>` (`after_generation`); other failures
  are REJECTED `invalid`.
- A repair may not drop tests or constraints (`generator/worker.py:_repair_guard`).

So a version gets at most 3 drafts of its world and 3 drafts of its task.
