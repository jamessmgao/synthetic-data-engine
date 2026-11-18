You are evolving a world that produced a task which was too easy. Five independent
agent runs solved it; a reviewer read their traces and found the strategy they used.
Your job is to change the WORLD itself, so that exactly that strategy no longer works,
while the world stays realistic, solvable and verifiable.

You work as root in a Linux container with a shell, in `/work` (scratch space).
Docker is available through the host's socket: label everything you start by hand
with `--label "$SDE_DOCKER_LABEL"` so it is cleaned up.

**Aim to be done in ~12 minutes; run the self-check by ~10.** Keep each step's reasoning
short: decide, act, check; change one module per response and rebuild before the next.
Keep the world small (about 200-500 lines in 2-3 modules; guidance, never a reason to
rewrite working code): change how its components interact, do not grow it. `/opt/sde/scaffold/` has stdlib helpers
(a scheduler, a fault injector, a repro harness); use them only if they save you time.

# The parent world

The world to evolve is in `$parent_dir/` (read-only):

- `world.json`: its spec, invariants, counterexample, plausible wrong fix and lineage;
- `environment/`: the system, with its Dockerfile;
- `repro/repro.sh`: reproduces the counterexample (exit 1 on the world, 0 once fixed);
- `world_fix/fix.sh`: makes every invariant hold.

$parent_summary

# Why it was too easy

$analysis
$solver_runs_line

The strategy that passed (it must FAIL on the world you build):

$target_fix

# The mutation to make

$operators_section

Why these: $why

The parent world's hardness checks (`sde-world check` runs them on your world too):

$hardness

Earlier mutations of this world (keep their effects; do not undo them):

$history

# What makes a good mutation

1. **It increases the reasoning required, not the size.** Adding unrelated files,
   bigger inputs alone, or more code does not count. The new difficulty must be
   causally relevant to the failure: it must be why the old strategy fails.
2. **It defeats the strategy that passed.** Apply `target_fix` (as you understand it
   from the analysis) to your new world and show that an invariant is still violated:
   write that check as a repro and run it. A mutation the old strategy still survives is
   not a mutation. The new difficulty must be something a careful rewrite still gets
   wrong, not only a minimal patch: a stated requirement that a from-scratch
   implementation meets naturally adds nothing (aws-agent-b1-t04 v2 went 5/5 again).
3. **The world stays solvable and checkable.** A new correct fix exists, written in
   `world_fix/fix.sh`; `repro/repro.sh` exits 1 on the new world and 0 after the fix,
   deterministically, every time.
4. **The defect is not announced.** No comments, logs, file names or README text that
   point at the new failure or describe its schedule (the step sequence, the trigger
   input or time step, where a fault fires relative to the effects), and no
   documented crash hooks or stage names. Any symptom the system shows is realistic.

**If the strategy that passed was a local fix** (one file, one function, one guard),
make a local fix insufficient, in this order of preference:
`extend_cross_component_dependency` (the correct fix must touch 2+ modules, or a
module elsewhere comes to depend on the current behavior, e.g. a derived value, a cache,
a report or a downstream consumer, so the local patch breaks it),
`introduce_global_invariant`, `generate_counterexample`. Whatever the operators above,
realize them in that spirit; size-adding changes (`expand_state_space`,
`add_realistic_distractor`) come last and never alone. The new plausible wrong fix
must still make the simple visible symptom go away and fail only on a deeper
schedule or on the dependent. Faults stay external: no documented crash hook or stage
names in the new world (remove any the parent has).

Techniques that deepen reasoning (use them to realize the operators above):

$techniques

# What you produce

Write the new world to `$output_dir/`, in the same layout as the parent:

- `environment/`, `repro/repro.sh`, `world_fix/fix.sh` (the same contract as a new world:
  `sde-world check` verifies them). A mutation is where difficulty is added, so also write
  `repro/symptom_check.sh` (the visible symptom only), `world_fix/wrong_fix.sh` (the NEW
  plausible wrong fix, implemented) and, if fix.sh changes only one file,
  `repro/dependent_check.sh`: `sde-world check` reports them as hardness WARNINGs;
- `world.json` with: the parent's spec; the invariants (updated if the mutation adds
  one); the NEW counterexample (sequence, `"reproduce": "repro/repro.sh"`, what was
  observed); and a NEW `plausible_wrong_fix` (the strategy that passed is a good
  candidate, if it is now wrong for a real reason).
- The pipeline writes `lineage` (the parent $parent_sha and every mutation so far).
  Only record what this mutation defeats, as
  `"lineage": {"mutations": [{"defeats": "<the strategy that passed, and why it now fails>"}]}`;
  anything else you put in `lineage` is replaced.

# Check before you finish

    sde-world check $output_dir       # build; repro fails twice, then passes twice after the fix

Also run your check that `target_fix` fails on the new world, and keep its output in
your notes. Your terminal is one interactive shell: never type `exit` or `set -e` at the
prompt (they end the session); put multi-step checks in a script or `bash -c '…'`. Finish only when `sde-world check` exits 0 and the old strategy fails.

`sde-world check $output_dir` must exit 0. You are not done until it does; if it fails, fix
the one failing check it names and re-run. Never finish with a failing or skipped check.
