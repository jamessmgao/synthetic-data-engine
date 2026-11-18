You are authoring one Terminal-Bench task from a world that has already been built and
checked. You work as root in a Linux container with a shell, in `/work` (empty scratch
space). Docker is available through the host's socket: label everything you start by
hand with `--label "$SDE_DOCKER_LABEL"` so it is cleaned up.

You have **20 minutes**. Have the draft written by about 12, then run the self-check
below and fix what it finds; finish within 20. The world is already built and checked,
so reuse it: the task's repository is the world's `environment/` as it is. Do not
grow it, rewrite it or add modules; the difficulty is already in it.

# The world

The world is in `/input/world/` (read-only reference; copy what you need into `/work`):

- `/input/world/world.json`: the world's invariants (what must always be true), a
  counterexample (a concrete sequence of events that violates one invariant, with the
  command that reproduces it), and `plausible_wrong_fix`, a fix a capable engineer
  would try that does NOT make the invariants hold.
- `/input/world/environment/`: the system itself, with its Dockerfile. It contains the defect.
- `/input/world/repro/repro.sh`: reproduces the counterexample deterministically. It
  exits 1 on the world as it is (an invariant is violated) and 0 once the world is fixed.
- `/input/world/world_fix/fix.sh`: a fix that makes every invariant hold.

The world has been checked: `repro.sh` fails on it and passes after `fix.sh`.

$world_summary

# What you produce

Write exactly one JSON object to `/logs/artifacts/draft.json` that matches the
TaskDraft schema. Run `sde-task schema` first: it prints that JSON schema, every field
with its type and constraints. Match it exactly (`sde-task check` validates against it).
The draft becomes the task:

- **The agent's repository** (`dockerfile`, `environment_files`): the world's
  `environment/`, with the defect still in it. Keep the system realistic. Do not
  include `repro/`, `world_fix/`, `world.json`, or anything else that reproduces the
  counterexample or describes the fix: no example scenario, script, log, comment or
  README that walks through the failing sequence. A happy-path example is fine.
- **The reference solution** (`solution_script`, plus `solution_files` if needed): a
  genuine fix that makes every invariant hold, based on `world_fix/fix.sh` and adapted
  to the task's layout.
- **The wrong fix** (`wrong_fix_script`): a concrete implementation of the world's
  `plausible_wrong_fix`, written the way a capable engineer would write it. The tests
  must reject it. Its first line is a comment naming it, with the text copied exactly
  from `world.json`:
  `# plausible_wrong_fix: <the plausible_wrong_fix text>` (`sde-task check` requires it).
- **The hidden tests** (`test_outputs`, `test_support_files`):
  1. the counterexample, replayed exactly as `repro.sh` does it, asserting the
     invariant it violates. `repro.sh` runs inside the image from its `WORKDIR` and
     reaches the system by absolute paths (`/app/...`); replay it the same way, so
     the test fails on the world exactly as the world check saw;
  2. the same invariants under other orders, inputs or failures, generated from a
     fixed seed, so a fix that only handles the replayed sequence fails;
  3. regression tests for the behavior that must keep working, so a fix that breaks
     something else fails;
  4. at least one test the wrong fix fails for a real reason (not a check on its text).
  Tests check outcomes through the system's interface, never source text.
- **The instruction** (`instruction`): the user's request. State the symptom users
  saw, the guarantees that must hold (the invariants, phrased as observable outcomes),
  everything that must keep working, and every value, path, command and format the
  tests rely on. Do NOT state the counterexample sequence, the cause, the files to
  change, or what the fix must do. Discovering the failing sequence is the task.
- **The spec** (`spec`): `correctness_constraints` are the invariants and the
  must-keep-working behaviors; `coverage` maps each to the tests that check it.

# Shape and difficulty

$shape_section

# Rules every task must follow

$system_rules

# Check before you finish

Run these, read the JSON they print, fix the draft, and repeat until both exit 0
(both commands check the draft against the world in `/input/world/`):

    sde-task check /logs/artifacts/draft.json            # {ok, errors}: the schema and every pipeline rule
    sde-task run /logs/artifacts/draft.json --as all     # oracle: reward 1; nop: reward 0; wrong: reward 0

`--as all` builds the task image once and runs each role in a fresh container
(`--as oracle`, `--as nop`, `--as wrong` alone re-run one role; an unchanged
`environment/` is not rebuilt). It runs them like the evaluation does (1 CPU, 2 GB, the
solution as the unprivileged `agent` user, the tests as root). Its JSON lists every
test by name with its status and assertion message (`tests`), and failures caused only
by setup problems (an import error, a missing file or command, no reward) under
`setup_errors`: a nop or wrong run that fails only that way does not count. Read
which tests failed:

- `nop`: the test that replays the world's counterexample MUST be among the failures,
  failing on the invariant the counterexample violates (the same outcome `repro.sh`
  observed). A nop that fails only on a missing file, an import error or a crash means
  the tests do not encode the counterexample: fix the tests.
- `wrong`: at least one test fails because an invariant is violated, not because of
  a missing file or a crash.

Then re-read your instruction as the agent would: could someone solve it from the
instruction alone, without guessing? Does any sentence give away the cause, the fix
or the failing sequence? Fix it if so. Finish only when every check passes; never
weaken or delete a test to make one pass.

$request
