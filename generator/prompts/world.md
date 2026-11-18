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

1. **Build the system first.** Several modules with real responsibilities (an API or
   CLI, state, persistence, a worker or scheduler, ...), not one file. It must run.
2. **State its invariants** before looking for a failure: the guarantees a correct
   implementation must preserve (durability of acknowledged writes, no oversell,
   exact semantics preserved, global optimality, ...). Two or more that interact
   are best: fixing one naively should risk another.
3. **Find the counterexample by EXECUTING the system.** Drive it with a deterministic
   scheduler, fault injector or crafted input until an invariant is violated, and
   record the exact sequence. Do not just reason about it: run it and watch it break.
4. **Make the reproduction deterministic.** `repro/repro.sh` replays the sequence and
   exits 1 when the invariant is violated, 0 when it holds. It must give the same
   answer every run (no sleeps-as-synchronization, no wall-clock races, fixed seeds).
5. **Write the real fix** in `world_fix/fix.sh` (it edits the world's code in place):
   afterwards repro.sh exits 0 and every invariant holds.
6. **Name the plausible wrong fix**: the change a capable engineer would most likely
   make first, which makes the visible symptom go away but still violates an
   invariant (e.g. a process-local dedup set where the guarantee must survive
   restarts). Check that it really fails on your world.

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
- `repro/repro.sh`: the deterministic reproduction (run inside the world's image, from `/`);
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

# Check before you finish

    sde-world check /logs/artifacts/world

It builds the world, runs repro.sh twice on it (must exit 1 both times) and twice
after world_fix/fix.sh (must exit 0 both times). Save its output:

    sde-world check /logs/artifacts/world > /logs/artifacts/world_check.json

Finish only when it exits 0.
