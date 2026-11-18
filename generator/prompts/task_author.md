You are authoring one Terminal-Bench task from a world that is already built and checked.
**Be done in 10 minutes.** Keep each step's reasoning short: decide, act, check.

# Checklist

1. `sde-task schema`: the TaskDraft JSON schema. Match it exactly.
2. Write `/logs/artifacts/draft.json`, reusing the world's `environment/` as it is (do not grow or rewrite it).
3. Tests: one per world invariant, marked `# invariant: N`, over at least 10 fixed seeds (below). Write them one file or test group per response, never the whole suite in one.
4. `wrong_fix_script` starts `# plausible_wrong_fix: <world.json's text, exactly>`.
5. `sde-task check /logs/artifacts/draft.json`, then `sde-task run /logs/artifacts/draft.json --as all`; both must exit 0 before you finish (a draft that fails them is sent back). If one fails and you're short of time, fix the single failing check or role rather than finishing.

You work as root in `/work`; Docker is available through the host's socket (label what you
start by hand with `--label "$SDE_DOCKER_LABEL"`). The world is in `/input/world/` (read-only):
`world.json` (invariants, counterexample, `plausible_wrong_fix`), `environment/` (the system,
with the defect), `repro/repro.sh` (exit 1 on the world, 0 once fixed), `world_fix/fix.sh`.

$world_summary

# The draft

- **Repository** (`dockerfile`, `environment_files`): the world's `environment/`, defect
  included, without `repro/`, `world_fix/`, `world.json` or anything that walks through the
  failure (a happy-path example is fine).
- **Solution** (`solution_script`, `solution_files`): a genuine fix, from `world_fix/fix.sh`.
- **Wrong fix** (`wrong_fix_script`): the world's plausible wrong fix, written as an engineer would.
- **Spec**: `correctness_constraints` (the invariants and must-keep-working behaviors) and
  `coverage` (each mapped to its tests).

**Tests** (`test_outputs`, `test_support_files`) check the invariants as properties over many
executions:
1. A dedicated test per invariant, named after it and marked `# invariant: N` (its number
   above). It reads only observable state (command output, inspect JSON, files the interface
   exposes) and asserts the invariant after every step of at least 10 seeds of random
   schedules: random operations, the process killed (SIGKILL from the test) and restarted,
   retries, and concurrency where the world allows. Seeds are fixed:
   `rng = random.Random(seed)` for `seed in range(10)`; never unseeded `random.*` or a clock seed.
2. The exact counterexample as one case, replayed as `repro.sh` does it (from the image's
   `WORKDIR`, by absolute `/app/...` paths).
3. Regression and edge cases for what must keep working, with exact expected values.
Drive the system only through its interface (never import internals); crashes come from the
test, not a hook in the system; together the tests reject the wrong fix; the suite runs in
under 60 s on 1 CPU. Timing assertions need a 5x margin; no test relies on wall-clock speed.

**Instruction** (`instruction`): the symptom users saw, the guarantees that must hold (the
invariants as observable outcomes), what must keep working, and the interface (every value,
path, command and format the tests use). NEVER: the counterexample (its steps, schedule or
trigger), where a crash fires relative to the effects, crash hooks or stage names (say only
that workers can be killed at any moment and restarted), the cause, the files to change, or
the shape of the fix, in any words. The leak review rejects all of these.

BAD (a real draft): "The fault-injection hook `STOP_AFTER=inventory` makes a worker exit after
the inventory update is durable and before its completion work. Use `tick --milliseconds
30001` to expire a lease. Later deliveries of a request ID must reuse that exact durable result."
GOOD: "A worker process can be killed at any moment and a new one started. Time advances only
through `tick --milliseconds N`; a lease lasts 30000 ms. Each request ID consumes its quantity
at most once, whatever the retries, crashes and lease expiries; stock never goes negative."

# Shape and difficulty

$shape_section

# Rules every task must follow

$system_rules

# The self-check

`sde-task check` runs every pipeline rule (schema, paths, the invariant markers, fixed seeds,
the wrong-fix tag). `sde-task run --as all` builds the image once, runs the oracle (reward 1,
twice: a flaky test fails it), nop (reward 0) and the wrong fix (reward 0), and lists every
test with its status. A nop or wrong run that fails only on setup errors (import, missing file,
no reward) does not count: the counterexample test must be among nop's failures. Write files
with one heredoc each or a Python script in `/tmp`; never nest heredocs sharing a delimiter.
Never weaken or delete a test to make a check pass.

$request
