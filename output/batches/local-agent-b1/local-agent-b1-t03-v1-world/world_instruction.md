You are building a software WORLD: a small, real, executable system in which a
capable engineer can meet a genuine technical obstacle. A separate step will later
write a task about it; your job is only the world, its invariants, and an
executable counterexample that proves one of them can be violated.

You work as root in a Linux container with a shell, in `/work` (scratch space).
Toolchains: Python 3.12, Go, gcc/make, a JDK 21, sqlite3, a Postgres client, git.
Docker is available through the host's socket: build and run the world in its own
containers. Through the socket a path in THIS container is not a path on the host,
so never use `docker run -v`: build with a context (it is uploaded) and move files
with `docker cp`. Label everything you start by hand with `--label "$SDE_DOCKER_LABEL"`
so it is cleaned up.

# This is a NEW world

Nothing exists yet: build it from the spec below.

# The spec

```json
{
 "dial": 3,
 "difficulty": {
  "dial": 3,
  "form": "behavior_preserving"
 },
 "environment": "Python service (SQLite)",
 "family": "database_consistency",
 "mechanism": "crash_between_commit_and_effect",
 "shape": "fix",
 "taxonomy": {
  "core_capability": "crash between commit and effect",
  "domain": "Database consistency",
  "environment": "Python service (SQLite)",
  "failure_mode": "crash between commit and effect",
  "skill_interaction": "multiple interacting constraints",
  "task_type": "Debugging"
 },
 "verification": "concurrent_integration"
}
```

Family: Database consistency and recovery.
- Where the difficulty comes from: isolation anomalies, transaction boundaries, crashes, concurrent updates, persistent state.
- Strongest lever: a failure or concurrent execution that violates a business invariant even though every individual database operation succeeds.
- Typical worlds: e.g. a booking, inventory, ledger or quota service on SQLite or Postgres, with concurrent clients, retries and a crash point between a commit and its side effect.
- The counterexample: two or more clients run in a fixed interleaving (or a crash is injected at a named statement) and the final state breaks the invariant, e.g. oversold stock or a double charge.
- Scaffolds (/opt/sde/scaffold): sde_sched.py to interleave the clients' transactions step by step and sde_fault.py for a crash between a commit and its side effect.
- Why a local patch fails here: the fix spans the write path and the retry / recovery worker, or a derived table, ledger report or cache is computed from the rows in the shape the bug writes them.
- Faults come from outside (kill the process at seeded points, a test-side harness): no documented crash hook or stage names.
- Scope: about 300-800 lines in 2-4 modules; the difficulty is in how they interact, not in size.

# How to build it (in this order)

1. **Build the system first.** 2-4 modules with real responsibilities (an API or
   CLI, state, persistence, a worker or scheduler, ...), not one file. It must run.
2. **State its invariants** before looking for a failure: the guarantees a correct
   implementation must preserve (durability of acknowledged writes, no oversell,
   exact semantics preserved, global optimality, ...). Two or more that interact
   are best: fixing one naively should risk another. Phrase each as an OUTCOME an
   observer can check ("each request ID debits its quantity at most once"), never as
   the mechanism that ensures it ("later deliveries must reuse the stored result"):
   the task's instruction is built from them, and a mechanism there is the fix.
3. **Find the counterexample by EXECUTING the system.** Drive it with a deterministic
   scheduler, fault injector or crafted input until an invariant is violated, and
   record the exact sequence. Do not just reason about it: run it and watch it break.
   Use the scaffolds below for this instead of writing your own.
4. **Make the reproduction deterministic.** `repro/repro.sh` replays the sequence and
   exits 1 when the invariant is violated, 0 when it holds. It must give the same
   answer every run (no sleeps-as-synchronization, no wall-clock races, fixed seeds).
5. **Write the real fix** in `world_fix/fix.sh` (it edits the world's code in place):
   afterwards repro.sh exits 0 and every invariant holds. **A local patch must not be
   enough.** Either the correct fix touches 2 or more modules, or a module elsewhere
   depends on the current (buggy) behavior: a derived value, a cache, a report, a
   downstream consumer, an ordering another stage relies on. Then the obvious one-file
   patch breaks that dependent. Earlier, the only task that beat the agents was like
   this: a purity fix in one module silently broke another module's totals, which had
   relied on the old side effect.
6. **Name the plausible wrong fix**: the change a capable engineer would most likely
   make first, and check it on your world. It must make the simple, visible symptom
   go away (the first reproduction an agent would write: one crash, one retry, the
   reported input) and fail only on a deeper schedule (another crash point, a second
   retry, a concurrent worker) or on the dependent. A wrong fix that does not even fix
   the visible symptom is not plausible; one that survives every schedule is not wrong.
   Example: a process-local dedup set fixes a retry within one process and fails when
   the worker is killed and restarted.

# Scaffolds: use these instead of writing your own

`/opt/sde/scaffold/` has small, tested, stdlib-only Python helpers (read its
`README.md`; a minute well spent). Copy the ones you use into `environment/` so the
world does not depend on this container:

- `sde_sched.py`: deterministic interleavings. Actors are generators that `yield` at
  each point another may run; a seed or a recorded schedule picks the order, and
  `explore(build, check, seeds)` returns the first schedule that breaks an invariant.
- `sde_fault.py`: planned faults: a crash at a named point (on its Nth hit), a dropped
  or duplicated message, a torn write, from a plan in `SDE_FAULTS`. The plan belongs to
  the repro and the tests: never document `SDE_FAULTS` in the README or the task, and
  give points neutral names (`p1`, `p2`, ... in code order would still reveal order:
  prefer one generic `faults.point("io")` before every write, with the plan choosing
  the Nth hit), so the point names do not map out the stages.
- `sde_repro.py`: the repro harness: replays `repro/sequence.json`, checks your
  invariants, exits 1 violated / 0 holds / 2 harness broken.

For C or Go worlds the README shows the same patterns to write inline (a crash point
read from the environment, goroutines released one step at a time). The scaffolds are
plumbing for the counterexample; the difficulty stays in the system's own components.

# Time and scope

You have **30 minutes**. Have the system written and running by about 10 minutes, the
counterexample replaying by about 20, then run the self-check and fix what it finds;
finish within 30. A world that runs out of time is lost.

Keep it to **about 300-800 lines of code in 2-4 modules**. Difficulty comes from
components that interact and from hidden dependents (code whose correctness relies on
another module's behavior, a caller that a naive fix breaks), never from size: earlier
worlds with large codebases and readable defects were solved anyway, and more code only
costs time.

# What the world must NOT be

- **Not a tool with 1-2 line planted bugs.** `=` instead of `+=`, an off-by-one, a
  missing lock around a counter, a textbook pattern an agent recognizes on sight:
  agents solve those in a few steps. The defect must be an interaction (between
  components, guarantees or steps in time) that a plausible implementation gets wrong.
- **Not announced.** No comments, log lines, file or function names, README text or
  TODOs that point at the defect or the fix. Symptoms are realistic, nothing more.
- **Not padded.** No unrelated files, filler data or obscure APIs: the difficulty is
  causal, not volume.
- **No documented fault hooks or stage names.** Do not give the system an env var,
  flag or command that crashes it at a named stage (`STOP_AFTER=inventory`): it tells
  the agent where the failure is and in what order the stages run. Inject faults
  from outside instead, the way the tests will: kill the process (SIGKILL) at seeded
  points from a test-side harness, or drive the scaffold's `sde_fault` from the
  repro and the tests. The system itself only has to survive being killed at any
  moment and restarted. If a hook is truly unavoidable, it must not reveal the order
  of the stages or which one matters.
- **Not dependent on unstated values.** Anything a later task will need (ports,
  credentials, paths, formats) is discoverable in the world itself.

# Constraints of the evaluation environment

- The world's containers get 1 CPU and 2 GB: no parallel-speedup assumptions; any
  timing bound needs a 5x margin.
- The world will later run as a non-root `agent` user, with the verifier as root;
  nothing may install packages at runtime (`apt`, `pip` in the Dockerfile only).
- Build pitfalls seen before: Go fails on unused imports; gcc 14 rejects implicit
  declarations; `strtok_r` needs `_POSIX_C_SOURCE`; Java lambdas need effectively
  final variables. Build it and fix the build.
- `environment/Dockerfile` must start `FROM` one of: `debian:bookworm-slim`, `eclipse-temurin:21-jdk-jammy`, `gcc:14-bookworm`, `golang:1.23-bookworm`, `node:22-bookworm-slim`, `postgres:16-bookworm`, `python:3.11-slim-bookworm`, `python:3.12-slim-bookworm`, `python:3.13-slim-bookworm`, `rust:1.83-slim-bookworm`, `ubuntu:24.04`. Every `COPY`
  source must exist in `environment/`.

# What you produce

Write the world to `/logs/artifacts/world/`:

- `environment/`: the system, with its `Dockerfile` (the system at `/app` is a good default);
- `repro/repro.sh`: the deterministic reproduction (the deep schedule);
- `repro/symptom_check.sh`: the visible symptom only, the first reproduction an agent
  would write (one crash, one retry, the reported input): exits 1 on the world, 0
  once the symptom is gone;
- `world_fix/fix.sh`: the real fix;
- `world_fix/wrong_fix.sh`: the plausible wrong fix, implemented as an engineer would
  write it (it edits the world in place, like fix.sh);
- `repro/dependent_check.sh` (needed when fix.sh changes only one file): the dependent
  module's behavior, exits 0 on the world and after fix.sh, 1 after wrong_fix.sh;
- `world.json`:

```json
{"spec": <the spec above>,
 "name": "<short-kebab-case-name>",
 "invariants": ["...", "..."],
 "counterexample": {"sequence": ["step 1", "step 2", "..."],
                    "reproduce": "repro/repro.sh",
                    "observed": "<what violates which invariant>"},
 "plausible_wrong_fix": "<one sentence: the fix most engineers would try first, and why it is wrong>"}
```

(The pipeline fills in `lineage`.)

Every script runs INSIDE the world's image, in a fresh container: they are copied to
`/world/repro/` and `/world/world_fix/` and started from the image's `WORKDIR`, so
refer to the system by absolute paths (`/app/...`). For the fixed world the check
runs `bash /world/world_fix/fix.sh && bash /world/repro/repro.sh` (and likewise
`wrong_fix.sh && ...`).

# Check before you finish

    sde-world check /logs/artifacts/world

It builds the world and checks, stopping at the first failure (exit codes other
than 0/1 mean a script itself failed):
- repro.sh exits 1 twice on the world, and symptom_check.sh exits 1;
- after fix.sh: repro.sh exits 0 twice, and symptom_check.sh exits 0;
- the fix is not local: fix.sh changes 2+ files of the system, or dependent_check.sh
  exits 0 on the world and after fix.sh, and 1 after wrong_fix.sh;
- after wrong_fix.sh: symptom_check.sh exits 0 (it fools a quick check) and
  repro.sh exits 1 (it is still wrong).
It saves its result to `/logs/artifacts/world_check.json`, which the pipeline reads:
a world without a passing self-check is rejected. The last three checks (symptom,
locality, wrong fix) may be reported as `WARNING:` lines instead of failing it; they
are measured either way, and a world that misses them makes an easier task. Fix every
WARNING before finishing if time allows.

Finish only when it exits 0.
