You are building a software WORLD: a small, real, executable system with a defect a
capable engineer can meet. A later step writes the task; you write only the world.

**Aim to be done in ~12 minutes; run the self-check by ~10.** Keep each step's reasoning
short: decide, act, check.

# Checklist (write to `/logs/artifacts/world/`)

1. `environment/`: the system, about 200-500 lines in 2-3 modules, with its `Dockerfile` (`FROM` one of: $base_images; system at `/app`). Write it one module per step (≤ ~150 lines per response) and build and run it before writing the next; never write the whole system, or more than one module, in one response. The line count is guidance, not a gate: never rewrite a working world to hit it; trim only if trivial.
2. Run it, then find the counterexample by EXECUTING it (a fixed schedule, an injected fault or a crafted input) and record the exact sequence.
3. `repro/repro.sh`: replays it deterministically; exit 1 = an invariant is violated, 0 = it holds.
4. `world_fix/fix.sh`: the real fix (edits the world in place); afterwards repro.sh exits 0.
5. `world.json`: `{"spec": <the spec below>, "name": "<kebab-case>", "invariants": [...], "counterexample": {"sequence": [...], "reproduce": "repro/repro.sh", "observed": "..."}, "plausible_wrong_fix": "..."}` (the pipeline fills `lineage`).
6. `sde-world check /logs/artifacts/world` must exit 0 before you finish (a world whose check fails is rejected). If it fails and you're short of time, fix the single failing check (the message says which) rather than finishing. Fix any WARNING it prints if time allows.

$origin_section

# The spec

```json
$spec_json
```

$family_brief

# Rules

- **Invariants are outcomes** an observer can check ("each request ID debits its quantity at
  most once"), never the mechanism that ensures it ("later deliveries reuse the stored
  result"): the task's instruction is built from them.
- **The defect is an interaction** (between components, guarantees or steps in time), not a
  textbook slip agents spot on sight (`=` for `+=`, an off-by-one, a missing lock around a
  counter), and the fix is not a one-liner. Prefer a fix that touches 2 modules, or a module
  elsewhere that depends on the current behavior.
- **The plausible wrong fix** is what a capable engineer would try first: it makes the visible
  symptom go away but still fails repro.sh, only on a deeper schedule (another crash point, a
  second retry, a concurrent worker) or on a dependent (e.g. a process-local dedup set that a
  restart forgets). Check it on your world.
- **No documented fault hooks or stage names** (no env var or flag that crashes the system at
  a named stage). Inject faults from outside: kill the process at chosen points from the
  repro, or drive a fault plan the system never documents.
- **Not announced, not padded**: no comments, logs, names or README text pointing at the
  defect; no filler. Anything a task will need (ports, paths, formats) is discoverable.
- **Deterministic**: no sleeps as synchronization, fixed seeds, the same answer every run.

# Environment

Root in a Linux container, in `/work`; Python 3.12, Go, gcc/make, JDK 21, sqlite3, a Postgres
client, git. Docker through the host socket: build and run the world in its own containers;
never `docker run -v` (paths here are not host paths): build with a context and move files
with `docker cp`. Label what you start by hand with `--label "$$SDE_DOCKER_LABEL"`. Clean up only
your own containers (`docker ps --filter label=$$SDE_DOCKER_LABEL`): never kill, stop or remove
containers you didn't start (the daemon is shared). If the terminal is stuck, press C-c once and
wait; never kill all containers. The world
later runs as a non-root `agent` user with 1 CPU / 2 GB; install packages only in the
Dockerfile. Any timing bound in tests needs a 5x margin (1 CPU). Pitfalls: every `COPY`
source must exist in `environment/`; when you replace or rename a module, delete the old file
(Go / C / Java: stale files break the build); Go fails on unused imports; gcc 14 rejects implicit
declarations; `strtok_r` needs `_POSIX_C_SOURCE`; Java lambdas need effectively final variables.

Scripts run inside the world's image, copied to `/world/repro/` and `/world/world_fix/` and
started from its `WORKDIR`: reach the system by absolute paths (`/app/...`). The check runs
repro.sh twice (exit 1 both times) and `fix.sh && repro.sh` twice (exit 0 both times); other
exit codes mean a script failed. It saves `/logs/artifacts/world_check.json`.

Optional (recorded as hardness when present): `repro/symptom_check.sh` (the visible symptom
only: exit 1 on the world, 0 after the fix), `world_fix/wrong_fix.sh` (the wrong fix,
implemented), `repro/dependent_check.sh`.

Scaffolds in `/opt/sde/scaffold/` (`README.md`; stdlib Python: `sde_sched.py` deterministic
interleavings, `sde_fault.py` planned crashes / dropped messages, `sde_repro.py` the repro
harness): use them only if they save you time, and don't read sources you won't use. Go or C
worlds: the README's last section has the inline patterns.
