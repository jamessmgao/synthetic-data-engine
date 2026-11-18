You are building a software WORLD: a small, real, executable system in which a
capable engineer can meet a genuine technical obstacle. A separate step will later
write a task about it; your job is only the world, its invariants, and an
executable counterexample that proves one of them can be violated.

You work as root in a Linux container with a shell, in `/work` (scratch space).
Toolchains: Python 3.12, Go, gcc/make, a JDK 21, sqlite3, a Postgres client, git.
Docker is available through the host's socket: build and run the world in its own
containers. Through the socket a path in THIS container is not a path on the host,
so never use `docker run -v`: build with a context (it is uploaded) and move files
with `docker cp`. Label everything you start by hand with `--label "$$SDE_DOCKER_LABEL"`
so it is cleaned up.

$origin_section

# The spec

```json
$spec_json
```

$family_brief

# How to build it (in this order)

1. **Build the system first.** 2-4 modules with real responsibilities (an API or
   CLI, state, persistence, a worker or scheduler, ...), not one file. It must run.
2. **State its invariants** before looking for a failure: the guarantees a correct
   implementation must preserve (durability of acknowledged writes, no oversell,
   exact semantics preserved, global optimality, ...). Two or more that interact
   are best: fixing one naively should risk another.
3. **Find the counterexample by EXECUTING the system.** Drive it with a deterministic
   scheduler, fault injector or crafted input until an invariant is violated, and
   record the exact sequence. Do not just reason about it: run it and watch it break.
   Use the scaffolds below for this instead of writing your own.
4. **Make the reproduction deterministic.** `repro/repro.sh` replays the sequence and
   exits 1 when the invariant is violated, 0 when it holds. It must give the same
   answer every run (no sleeps-as-synchronization, no wall-clock races, fixed seeds).
5. **Write the real fix** in `world_fix/fix.sh` (it edits the world's code in place):
   afterwards repro.sh exits 0 and every invariant holds.
6. **Name the plausible wrong fix**: the change a capable engineer would most likely
   make first, which makes the visible symptom go away but still violates an
   invariant (e.g. a process-local dedup set where the guarantee must survive
   restarts). Check that it really fails on your world.

# Scaffolds: use these instead of writing your own

`/opt/sde/scaffold/` has small, tested, stdlib-only Python helpers (read its
`README.md`; a minute well spent). Copy the ones you use into `environment/` so the
world does not depend on this container:

- `sde_sched.py`: deterministic interleavings. Actors are generators that `yield` at
  each point another may run; a seed or a recorded schedule picks the order, and
  `explore(build, check, seeds)` returns the first schedule that breaks an invariant.
- `sde_fault.py`: planned faults: a crash at a named point (on its Nth hit), a dropped
  or duplicated message, a torn write, from a plan in `SDE_FAULTS`.
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
- `environment/Dockerfile` must start `FROM` one of: $base_images. Every `COPY`
  source must exist in `environment/`.

# What you produce

Write the world to `/logs/artifacts/world/`:

- `environment/`: the system, with its `Dockerfile` (the system at `/app` is a good default);
- `repro/repro.sh`: the deterministic reproduction;
- `world_fix/fix.sh`: the real fix;
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

Both scripts run INSIDE the world's image: they are copied to `/world/repro/repro.sh`
and `/world/world_fix/fix.sh` and started from the image's `WORKDIR`, so refer to the
system by absolute paths (`/app/...`). For the fixed world the check runs
`bash /world/world_fix/fix.sh && bash /world/repro/repro.sh`.

# Check before you finish

    sde-world check /logs/artifacts/world

It builds the world, runs repro.sh twice on it (must exit 1 both times; any other
exit code means the script itself failed) and twice after world_fix/fix.sh (must
exit 0 both times). It saves its result to `/logs/artifacts/world_check.json`, which
the pipeline reads: a world without a passing self-check is rejected.

Finish only when it exits 0.
