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
- **The hidden tests** (`test_outputs`, `test_support_files`). They check the world's
  INVARIANTS as properties over many executions, not one replayed scenario:
  1. **One dedicated test per invariant in world.json**, named after it
     (`test_invariant_no_double_debit`) and marked with a comment line
     `# invariant: N` (N = its number in the list above; `sde-task check` requires
     every invariant to be marked), so a failure says which guarantee broke. Its
     checker reads only observable state (command output, the inspect/report JSON,
     files the interface exposes) and asserts the invariant **after every step** of
     **at least 20 fixed seeds** of randomized schedules: random operations, the
     process killed (SIGKILL from the test, at a random step or write) and restarted,
     retries, lease expiries, and concurrent workers wherever the world allows them.
     Use fixed seeds (`rng = random.Random(seed)` for `seed in range(20)`, never the
     global `random.*` functions unseeded or a time-based seed: `sde-task check` rejects
     those, and `sde-task run --as all` runs the oracle twice and requires identical
     results); keep the whole suite under 60 s on 1 CPU (small schedules, many seeds).
  2. **The exact counterexample** as one case among them, replayed as `repro.sh` does
     it (from the image's `WORKDIR`, by absolute paths `/app/...`), so the test fails
     on the world exactly as the world check saw.
  3. **The dependent**: if the world has a module that relies on the current
     behavior, a test of that module's output, so a one-file patch fails.
  4. **Regression and edge cases** for everything the instruction says must keep
     working: the normal path, empty and boundary inputs, invalid input, the largest
     input within the limits. **Exact expected values**, never "is not None" or "> 0".
  5. Together they reject the wrong fix for a real reason (an invariant it violates),
     and no test passes on an empty or stub implementation.
  Tests drive the system only through its interface (its CLI, API or files), never by
  importing internals the agent could restructure, and never by reading source text.
  Crashes come from the test (kill the process), not from a hook in the system.
- **The instruction** (`instruction`): the user's request. State the symptom users
  saw, the guarantees that must hold (the invariants, phrased as observable outcomes),
  everything that must keep working, and the interface: every value, path, command
  and format the tests rely on. Do NOT state the counterexample (its steps, schedule
  or the input or time step that triggers it), where a crash or fault hook fires
  relative to the system's effects, the cause, the files to change, or the shape of
  the fix. Do not document crash hooks or stage names: say only that workers (or the
  service) can be killed at any moment and are restarted, and the tests kill them
  from outside. A clock command or other control the tests need is documented by its
  interface only, never which use of it breaks the system. Discovering the failing
  sequence is the task. The leak review rejects all of these.

  BAD (an earlier draft; each sentence hands the agent part of the counterexample or the fix):
  > The documented fault-injection hook is `STOP_AFTER=inventory`: when a claimed
  > request can be fulfilled, a worker exits with status `70` after the accepted
  > inventory movement and stock update are durable and before the worker's
  > remaining completion work. Use `tick --milliseconds 30001` to expire a lease.
  >
  > Once a request ID has produced an inventory movement, any later delivery of that
  > same request ID must reuse that exact durable result without applying another
  > inventory effect.

  GOOD (the same guarantees and interface; no hook, no stages, no schedule, no fix):
  > A worker process can be killed at any moment (including in the middle of a
  > write) and a new one started. Time advances only through
  > `tick --milliseconds N`; a lease lasts 30000 ms.
  >
  > Each request ID consumes its submitted quantity at most once, whatever the
  > retries, crashes and lease expiries; stock never goes negative; and a request
  > that fits the stock is eventually fulfilled.

  BAD tests: one test that replays the single crash from the counterexample and checks
  the final stock. GOOD tests: `test_invariant_at_most_one_debit_per_request` and
  `test_invariant_stock_never_negative` each run 20 seeds of random submits, workers
  killed at random steps, restarts and ticks, checking the invariant through
  `inspect --json` after every step; the counterexample is one fixed case in them.
- **The spec** (`spec`): `correctness_constraints` are the invariants and the
  must-keep-working behaviors; `coverage` maps each to the tests that check it.

# Shape and difficulty

$shape_section

# Rules every task must follow

$system_rules

# Writing files

Write each file with its own heredoc (`cat > /work/x.py <<'EOF'` … `EOF`), or with a
short Python script saved to `/tmp` that writes the files (and builds draft.json with
`json.dump`, so its strings are escaped for you). Never nest heredocs that share a
delimiter: the inner `EOF` ends the outer one, and the quoting error costs minutes.

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
instruction alone, without guessing? Does any sentence give away the cause, the fix,
the failing sequence, or where a fault hook fires relative to the effects? Fix it if so. Finish only when every check passes; never
weaken or delete a test to make one pass.

$request
