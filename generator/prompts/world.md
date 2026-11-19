You are building a software WORLD: a small, real, executable system with a defect a
capable engineer can meet. A later step writes the task; you write only the world.

**Aim to be done in ~12 minutes; run the self-check by ~10.** Keep each step's reasoning
short: decide, act, check.

# Checklist (write to `/logs/artifacts/world/`)

1. `environment/`: the system, about 200-500 lines in 2-3 modules, with its `Dockerfile` (`FROM` one of: $base_images; system at `/app`; no ENTRYPOINT/CMD that takes arguments: the check runs `bash` scripts in it). Write it one module per step (≤ ~150 lines per response), never the whole system in one response; build again after every module with `docker build -q -t "sde-dev-$${SDE_DOCKER_LABEL#sde.job=}" environment/ && echo BUILD_OK` (your own tag: other sessions share this Docker) and wait only until BUILD_OK. The line count is guidance: never rewrite a working world to hit it.
2. Run it, then find the counterexample by EXECUTING it (a fixed schedule, an injected fault or a crafted input) and record the exact sequence.
3. `repro/repro.sh`: replays it deterministically; exit 1 = an invariant is violated, 0 = it holds.
4. `world_fix/fix.sh`: the real fix (edits the world in place); afterwards repro.sh exits 0.
5. `world.json`: `{"spec": <the spec below>, "name": "<kebab-case>", "invariants": [...], "counterexample": {"sequence": [...], "reproduce": "repro/repro.sh", "observed": "..."}, "plausible_wrong_fix": "..."}` (the pipeline fills `lineage`).
6. `sde-world check /logs/artifacts/world` must exit 0 before you finish (a world whose check fails is rejected). If it fails and you're short of time, fix the single failing check (the message says which) rather than finishing. Fix any WARNING it prints if time allows.

$origin_section

# The spec

$domain_line
$mechanism_line

```json
$spec_json
```

$family_brief

# Rules

- **Invariants are outcomes** an observer can check ("each request ID debits its quantity at
  most once"), never the mechanism that ensures it: the task's instruction is built from them.
- **The defect is an interaction** (between components, guarantees or steps in time), not a
  textbook slip agents spot on sight (`=` for `+=`, an off-by-one, a missing lock around a
  counter), and the fix is not a one-liner. Prefer a fix that touches 2 modules, or a module
  elsewhere that depends on the current behavior; design that cause up front, never pad a
  second file to clear `fix_not_local`.
- **The correct behavior is not already in the system**: no second mode, reference path or
  recompute-from-scratch routine the fix can call (else the invariant names the fix). A
  reference may exist only in the tests or the repro, never in `/app`.
- **Not the textbook missing piece.** If the honest guarantee implies a standard remedy (store
  the idempotency key with the effect, a scope chain, an index), that remedy is already in the
  system, subtly wrong (a separate write, after the ack, the wrong key). Else the instruction names the fix.
- **The plausible wrong fix** is what a capable engineer would try first: it makes the visible
  symptom go away but still fails repro.sh, only on a deeper schedule (another crash point, a
  second retry, a concurrent worker) or on a dependent. Check it on your world.
- **No documented fault hooks or stage names**: inject faults from outside (kill the process
  from the repro, or a fault plan the system never documents).
- **Not announced, not padded**: no comments, logs, names or README text pointing at the
  defect, no filler. Names and docstrings on the defective path say what the code is for,
  never the limit that breaks it (BAD: `readAll`, "replays only committed entries");
  `sde-world check` warns (`limit_named`).
- **Deterministic**: no sleeps as synchronization, fixed seeds, the same answer every run.

# Environment

Root in a Linux container, in `/work`; Python 3.12, Go, gcc/make, JDK 21, sqlite3, a Postgres
client, git. Docker through the host socket, shared with other sessions: never `docker run -v`
(paths here are not host paths); label what you start with `--label "$$SDE_DOCKER_LABEL"` and
clean up only your own (`docker ps --filter label=$$SDE_DOCKER_LABEL`). If the terminal is stuck,
press C-c once and wait. Keep each command under ~4 KB: split any file over ~120 lines into 2+
writes (`cat > f <<'EOF'`, then `cat >> f <<'EOF'`), never nest heredocs, and patch code with a
script file (`cat > /tmp/p.py <<'EOF'` … then `python3 /tmp/p.py`), never inline Python with
nested `'''`. Never type `exit` or `set -e` at the prompt (they end the session). The world
later runs as a non-root `agent` user with 1 CPU / 2 GB; install packages only in the
Dockerfile. Any timing bound in tests needs a 5x margin (1 CPU). Pitfalls: every `COPY`
source must exist in `environment/`; `COPY a b c dest` needs `dest/`; when you replace or
rename a module, delete the old file (Go / C / Java: stale files break the build); Go fails on
unused imports; gcc 14 rejects implicit declarations; `strtok_r` needs `_POSIX_C_SOURCE`.

Test with `sde-world check`: it copies `repro/` and `world_fix/` into `/world/repro/` and
`/world/world_fix/` in an image FROM yours, runs them with bash from its `WORKDIR` (reach the
system at `/app/...`), repro.sh twice (exit 1) and `fix.sh && repro.sh` twice (exit 0), and saves
`/logs/artifacts/world_check.json`; don't read its source. By hand, `docker cp` scripts in and
run them as files (reach services at 127.0.0.1 inside the container); never pipe a script or
heredoc into `docker exec`/`run`.

Optional (recorded as hardness when present): `repro/symptom_check.sh` (the visible symptom
only: exit 1 on the world, 0 after the fix), `world_fix/wrong_fix.sh` (the wrong fix,
implemented), `repro/dependent_check.sh`.

Scaffolds in `/opt/sde/scaffold/` (`README.md`; stdlib Python: `sde_sched.py` deterministic
interleavings, `sde_fault.py` planned crashes / dropped messages, `sde_repro.py` the repro
harness): use them only if they save you time, and don't read sources you won't use. Go or C
worlds: the README's last section has the inline patterns.

# Before you finish

Never finish with a failing or skipped check: re-run `sde-world check` after each fix. While a
build or the check runs, wait with `"duration": 60` per step, never short 10 s polls.
