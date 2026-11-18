You are authoring one Terminal-Bench task from a world that has already been built and
checked. You work as root in a Linux container with a shell, in `/work` (empty scratch
space). Docker is available through the host's socket: label everything you start by
hand with `--label "$SDE_DOCKER_LABEL"` so it is cleaned up. Take your time: build the
task, run it, and check it before you finish.

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

Family: database_consistency; mechanism: isolation_anomaly; environment: Python service (SQLite); verification: fault_injection.

Invariants:
- For each SKU, confirmed reservations never exceed the stock
- A retried reservation request (same request id) never reserves twice
- A confirmed reservation survives a service restart

Counterexample (hidden from the agent):
- client A: reserve sku-1 (stock 1), reads available=1
- client B: reserve sku-1, reads available=1
- A inserts its reservation and commits
- B inserts its reservation and commits
Reproduce: repro/repro.sh
Observed: 2 confirmed reservations for sku-1, stock 1

Plausible wrong fix (the task's wrong_fix_script): wrap the availability check and the insert in one transaction at the default isolation

# What you produce

Write exactly one JSON object to `/logs/artifacts/draft.json` that matches the
TaskDraft schema (`sde-task check` validates it). It becomes the task:

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
     invariant it violates;
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

Shape: fix. The agent's repository is the world with its defect. The agent must make every invariant hold under every execution the world allows, without breaking anything else that works today.

Difficulty dial: hidden dependents of the changed behavior, level 2 of 3: Two modules rely on the old behavior; at least one dependency is invisible at the call site.

The dial sets how much of the world's difficulty reaches the agent. It never permits hiding a requirement: everything the tests check is stated in the instruction.

# Rules every task must follow

You write tasks for Harbor, the harness that runs Terminal-Bench 2. A task is a
Docker environment, an instruction, a reference solution, and pytest tests. An
AI agent receives only the instruction and a shell inside the container; the
tests then decide whether it succeeded.

Return exactly one JSON object that matches the provided schema. A pipeline
turns that object into files with deterministic code, then builds the image,
runs your reference solution (it must pass every test), and runs the untouched
environment (it must fail). Anything that cannot be built or run as written is
rejected.

## How Harbor runs your task

1. The image is built from `dockerfile` with `environment/` as the build
   context. The context contains exactly the files in `environment_files`
   (paths relative to `environment/`), so every `COPY`/`ADD` source must be one
   of them.
2. The agent works in a shell that starts in the image's `WORKDIR`, as the
   unprivileged user `agent`. The pipeline appends the lines that create this
   user and give it ownership of `WORKDIR` and of every directory listed in
   `agent_writable_paths`; everything else stays root-owned. So the agent
   cannot `apt-get install` or write system locations: install everything
   the task needs in the Dockerfile, and list any data or state directory
   outside `WORKDIR` that the system writes to (for example
   `/var/lib/myservice`). The agent sees the container and the instruction,
   nothing else. It never sees `tests/`, `solution/`, or your `spec`.
3. After the agent finishes, `tests/` is copied to `/tests` and the pipeline's
   own `tests/test.sh` runs `pytest /tests/test_outputs.py` from `WORKDIR`. You
   do not write `test.sh`.
4. pytest runs in an isolated Python 3.13 interpreter that has only the
   standard library and pytest. `test_outputs` therefore may import only the
   standard library, `pytest`, and helper modules you put in
   `test_support_files`. It must not import the application's modules or its
   third-party dependencies. Exercise the system the way a user would: run the
   container's own interpreter and tools through `subprocess` (for example
   `subprocess.run(["python3", "-m", "jobqueue.cli", ...], cwd="/app",
   timeout=60)`), and inspect files, databases, sockets and exit codes.
5. The reference solution is the body of `solution/solve.sh` (the pipeline adds
   the shebang). Harbor runs it as the same `agent` user in `WORKDIR` on a
   fresh container, then runs the tests as root. Files in `solution_files`
   are available under `/solution/<path>` while it runs. It has the same
   permissions as the agent, so it must not need root either.

## Rules the pipeline checks

- `name` is a lowercase slug (letters, digits, hyphens), 4-50 characters.
- `dockerfile`: every `FROM` uses one of these base images, written exactly
  like this and without a digest (the pipeline pins it): `debian:bookworm-slim`, `eclipse-temurin:21-jdk-jammy`, `gcc:14-bookworm`, `golang:1.23-bookworm`, `node:22-bookworm-slim`, `postgres:16-bookworm`, `python:3.11-slim-bookworm`, `python:3.12-slim-bookworm`, `python:3.13-slim-bookworm`, `rust:1.83-slim-bookworm`, `ubuntu:24.04`.
  Set a `WORKDIR`, pin every package version (`pip install flask==3.1.0`,
  never a bare name or a range), and do not add a `USER` line. Never copy
  tests or the solution into the image, and never put the answer anywhere
  the agent can read it.
- `agent_writable_paths`: absolute directories outside `WORKDIR` that the
  system writes at runtime; never system directories such as `/usr` or
  `/etc`.
- `environment_files` must not contain `Dockerfile`; `test_support_files` must
  not contain `test.sh` or `test_outputs.py`; `solution_files` must not contain
  `solve.sh`. Those have their own fields or are written by the pipeline.
- All Python must compile and all shell must pass `bash -n`.
- Compiled code must build cleanly with the image's compiler, both the
  starting code (the Dockerfile builds it) and the code after `solve.sh`. The
  compilers are strict: Go rejects unused imports and variables; gcc 14 treats
  implicit function declarations and int/pointer conversions as errors, so
  include every header you use and define `_POSIX_C_SOURCE` or `_GNU_SOURCE`
  before using POSIX functions such as `strtok_r`; Java lambdas may capture
  only effectively-final locals. Keep the planted defect a behavior bug, not a
  compile error, unless the task is about the build.
- **The container has 1 CPU and 2 GB of memory**, for the agent and for the
  tests. Never assert parallel speedup or throughput scaling (8 threads cannot
  beat 1 thread on one CPU), and never assert wall-clock or throughput
  thresholds that the reference solution doesn't clear by at least 5x in this
  container. Prove concurrency correctness with invariants under concurrent
  load, not with speed.
- `test_outputs` defines at least 3 test functions.
- `wrong_fix_script` is the plausible but wrong fix a capable engineer would
  most likely write first: it makes the visible symptom go away but breaks a
  stated requirement (for example a process-local dedup set where the
  requirement survives restarts, or a local reorder that breaks a dependent
  module). The pipeline runs it exactly like the reference solution and
  rejects the task if the tests accept it. It must differ from
  `solution_script`.
- Every constraint in `spec.correctness_constraints` appears in `coverage`
  with the exact names of the test functions that verify it.
- `instruction` never mentions tests, grading, `/tests`, `/solution`,
  `solve.sh`, `test.sh` or `test_outputs`.
- The starting code in `environment_files` contains no comments that point
  at the defect (`# BUG`, `FIXME`, "the bug is here", and so on). The agent
  reads that code; it has to find the problem itself.

## What makes the task good

- **The instruction is the whole contract.** It states the objective, the
  constraints, every path, command, interface and output format the tests
  rely on. A test that checks something the instruction does not state makes
  a failure the task's fault, not the agent's. This includes every value the
  tests or their helper scripts use: user names, passwords, hostnames, ports,
  file names, and how the tests start or connect to the system. If the tests
  log in as `git` with password `password`, the instruction must say so.
- **It does not give the answer away.** Describe the symptoms and the required
  guarantees, not the root cause or the patch. Do not list "key files", the
  functions or helpers to use, the order of operations to check, or an
  "approach" section: finding where the problem lives is part of the task.
  Describe the system's layout and interface (how to run it, where its data
  lives) only as far as a user of the system would know it.
- **Tests check behavior, not text.** Run the system and observe outcomes. Do
  not grep source code for an expected fix; many correct fixes exist.
- **Faults are deterministic and owned by the tests.** Trigger crashes and
  failures from the test side where possible (for example kill a worker
  process with SIGKILL at a point the test observes), and assert that the
  fault actually happened, so an agent cannot pass by disabling the fault
  path.
- **Shortcuts fail.** Hard-coded outputs, deleting data, skipping work, or
  weakening a guarantee to make one test pass must fail another test.
- **The untouched environment fails at least one substantive test**, not just
  a missing-file check, and the reference solution passes all of them.
- **Everything is deterministic.** No reliance on wall-clock races, random
  seeds you do not fix, or network services outside the container. Every
  subprocess call in a test has a timeout, and the whole test file finishes
  well under 10 minutes.
- **The environment is realistic.** Code, configuration and layout should read
  like a real project that someone maintains, not a puzzle. Do not pad it with
  irrelevant files.
- **The solution solves it for real.** `solve.sh` applies a genuine fix (for
  example with a heredoc, a patch, or a small Python edit script), not by
  editing tests or writing expected outputs directly. Many correct fixes may
  exist; the tests accept all of them, and the instruction prescribes none.
- **Difficulty comes from the engineering.** Do not add unrelated work,
  irrelevant files, or artificial complexity. Cover the happy path and the
  failure cases.
- **It is original.** Do not reproduce a public or human-written benchmark
  task, including the well-known examples.

## Mistakes earlier drafts made (avoid these)

The pipeline checks each draft for these before any agent sees it. A leak
reviewer compares the instruction with your hidden `spec` and rejects a draft
whose instruction reveals the cause or the fix.

1. **Requirements that restate the fix.** The reader must learn *what* must
   be true, never *what to change*.
   - BAD: "The consistency check must compare hourly totals against daily
     totals computed independently from raw events, and the `carry_over`
     enrichment must keep working." (That is the patch, and it names the
     internal field that holds the bug.)
   - GOOD: "`report.json` must flag every day whose hourly durations do not
     add up to that day's total duration in the input events (tolerance
     0.001 s)."
2. **Requirements that point at each planted bug.** Stating the general rule
   is required; singling out the broken case is a hint.
   - BAD: "All entries must be present, including transactions with three or
     more entries. A void commit includes `type=VOID ref=N`, and the
     referenced transaction must also be marked void." (Each sentence names a
     planted bug.)
   - GOOD: "The recovered database contains every account, transaction and
     entry recorded in the operation log, with the statuses the log implies
     (see the log format above)."
3. **The planted-bug template.** A provided tool with one or two one-line
   bugs (`total = x` for `total += x`, `max()` for `sum()`, the acknowledge
   before the write) plus requirements that describe each bug. A capable
   agent solves that in a few minutes. Build the task in the form the user
   message asks for, where the difficulty comes from how parts of the system
   depend on each other, not from finding a typo.
4. **Hint sections and markers.** "Key Files to Investigate", "Approach",
   "Hints", or `# BUG:` comments in the starting code. (Rejected by the
   schema.)
5. **Unstated test values.** A test that logs in with a password, connects
   to a port, or reads a file the instruction never mentions. (The task
   becomes impossible for the wrong reason.)

# Check before you finish

Run these, read the JSON they print, fix the draft, and repeat until all four exit 0
(both commands check the draft against the world in `/input/world/`):

    sde-task check /logs/artifacts/draft.json              # {ok, errors}: the schema and every pipeline rule
    sde-task run /logs/artifacts/draft.json --as oracle    # reference solution: reward 1, every test passes
    sde-task run /logs/artifacts/draft.json --as nop       # untouched repository: reward 0
    sde-task run /logs/artifacts/draft.json --as wrong     # the wrong fix: reward 0

`run` builds the task image and runs it like the evaluation does (1 CPU, 2 GB, the
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
