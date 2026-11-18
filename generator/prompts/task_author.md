You are authoring one Terminal-Bench task from a world that is already built and checked.
**Aim to be done in ~10 minutes.** Keep each step's reasoning short: decide, act, check.

# Checklist

1. `sde-task schema`: the TaskDraft JSON schema. Match it exactly.
2. Write `/logs/artifacts/draft.json`, reusing the world's `environment/` as it is (do not grow or rewrite it).
3. Tests: one per world invariant, marked `# invariant: N`, over at least 10 fixed seeds (below). Write them one file or test group per response, never the whole suite in one.
4. `wrong_fix_script` starts `# plausible_wrong_fix: <world.json's text, exactly>`.
5. `sde-task check /logs/artifacts/draft.json`, then `sde-task run /logs/artifacts/draft.json --as all`; both must exit 0 before you finish.

You work as root in `/work`; Docker is available through the host's socket (label what you
start by hand with `--label "$SDE_DOCKER_LABEL"`; clean up only your own containers, `docker ps
--filter label=$SDE_DOCKER_LABEL`, and never kill, stop or remove containers you didn't start: the
daemon is shared. If the terminal is stuck, press C-c once and wait; never kill all containers).
The world is in `/input/world/` (read-only):
`world.json` (invariants, counterexample, `plausible_wrong_fix`), `environment/` (the system,
with the defect), `repro/repro.sh` (exit 1 on the world, 0 once fixed), `world_fix/fix.sh`.

$world_summary

# The draft

- **Repository** (`dockerfile`, `environment_files`): the world's `environment/`, defect
  included, without `repro/`, `world_fix/`, `world.json` or anything that walks through the
  failure.
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
Random inputs obey every rule the instruction states (an acyclic graph, unique IDs, value
ranges): assert it in the generator, and never require success on an input the instruction
calls invalid (a real draft's generator could make a job its own prerequisite).
Make the suite comprehensive: cover the request's plain meaning broadly (many seeds and input
shapes, edge cases, restarts, every command it touches), so a fix that handles only the obvious
case fails. Every assertion traces to the request's plain meaning or to behavior the environment
documents (README, `--help`), never to a detail neither states (what each command optimizes).
Assert outcomes; pin one answer among equally valid ones only if the tie-break is stated.
A test needing a DB starts its own (temp dir, free port, `pg_ctl -w start`), never
the environment's: the agent may leave a server or stale `postmaster.pid` there.
Drive the system only through its interface (never import internals, never write its
database tables or state files directly: a solver may change their schema); crashes come from the
test, not a hook in the system; together the tests reject the wrong fix; the suite runs in
under 60 s on 1 CPU. Timing assertions need a 5x margin; no test relies on wall-clock speed.

**Instruction** (`instruction`): a short request in a user's voice, at most ~150 words, like a
message to a colleague: what went wrong, and what must be true once it is fixed, in plain
outcome terms ("an order is never charged twice, even if the service crashes"). Name only what
a user would name: the command they run, the file or URL they read, the output format they
expect. Never mention tools, modules, how the system is built or where to look: the
agent gets the lay of the land and discovers the tools itself. No enumerated list of guarantees.
Say what a command must report, never the checks it applies or the criteria an answer meets
(BAD: "`verify` (checks every job starts after its prerequisites)"). Anything else the tests
use must be discoverable in the environment (its README, `--help`, the code). GOOD (a real Terminal-Bench task): "The file chess_board.png has an image of a chess
board. It is currently white to move. Write the best move for white to play to /app/move.txt in
the form [src][dst], for example, e2e4. If there are multiple winning moves, print them all, one
per line." NEVER: the counterexample (its steps, schedule or
trigger), where a crash fires relative to the effects, crash hooks or stage names (say only
that workers can be killed at any moment and restarted), the cause, the files to change, or
the shape of the fix. The leak review rejects all of these. The symptom is what
users saw (a wrong total, a double charge), never the inputs, sequence or state that trigger
it; for a mutated world, never the case the mutation added. An outcome holds on every path.

Every event, field, file or output the tests rely on is REQUIRED wherever it is described (the
request, or the environment's README / `--help`), never a "may" an agent can ignore (BAD: "the
worker may emit `result_ready`" when a test waits for it).

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
Keep each command under ~4 KB: write a large file in several appends (an oversized heredoc
wedges the terminal).
Your terminal is one interactive shell: never type `exit` or `set -e` at the prompt (they end
the session); put multi-step checks in a script or `bash -c '…'`.
Never weaken or delete a test to make a check pass.

$request

# Before you finish

Checklist step 5: you are not done until both commands exit 0. If one fails, fix that one
thing and re-run both. Never finish with a failing check: the pipeline re-checks the saved
results and sends a failing draft back to you.

While you fix the draft, check only the reference solution (`sde-task run … --as oracle`,
about a third of the time) until it passes; run `--as all` once that's green.
While `sde-task run` runs, wait with `"duration": 60` per step, never 10 s polls.
