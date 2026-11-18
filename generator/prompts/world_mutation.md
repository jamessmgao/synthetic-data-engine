You are evolving a world that produced a task which was too easy. Five independent
agent runs solved it; a reviewer read their traces and found the strategy they used.
Your job is to change the WORLD itself, so that exactly that strategy no longer works,
while the world stays realistic, solvable and verifiable.

You work as root in a Linux container with a shell, in `/work` (scratch space).
Docker is available through the host's socket: label everything you start by hand
with `--label "$SDE_DOCKER_LABEL"` so it is cleaned up.

# The parent world

The world to evolve is in `$parent_dir/` (read-only):

- `world.json`: its spec, invariants, counterexample, plausible wrong fix and lineage;
- `environment/`: the system, with its Dockerfile;
- `repro/repro.sh`: reproduces the counterexample (exit 1 on the world, 0 once fixed);
- `world_fix/fix.sh`: makes every invariant hold.

$parent_summary

# Why it was too easy

$analysis

The strategy that passed (it must FAIL on the world you build):

$target_fix

# The mutation to make

$operators_section

Why these: $why

Earlier mutations of this world (keep their effects; do not undo them):

$history

# What makes a good mutation

1. **It increases the reasoning required, not the size.** Adding unrelated files,
   bigger inputs alone, or more code does not count. The new difficulty must be
   causally relevant to the failure: it must be why the old strategy fails.
2. **It defeats the strategy that passed.** Apply `target_fix` (as you understand it
   from the analysis) to your new world and show that an invariant is still violated:
   write that check as a repro and run it. A mutation the old strategy still survives is
   not a mutation.
3. **The world stays solvable and checkable.** A new correct fix exists, written in
   `world_fix/fix.sh`; `repro/repro.sh` exits 1 on the new world and 0 after the fix,
   deterministically, every time.
4. **The defect is not announced.** No comments, logs, file names or README text that
   point at the new failure. Any symptom the system shows is realistic.

Techniques that deepen reasoning (use them to realize the operators above):

$techniques

# What you produce

Write the new world to `$output_dir/`, in the same layout as the parent:

- `environment/`, `repro/repro.sh`, `world_fix/fix.sh`;
- `world.json` with: the parent's spec; the invariants (updated if the mutation adds
  one); the NEW counterexample (sequence, `"reproduce": "repro/repro.sh"`, what was
  observed); a NEW `plausible_wrong_fix` (the strategy that passed is a good candidate,
  if it is now wrong for a real reason); and `lineage` =
  `{"parent_world_sha256": "$parent_sha", "mutations": [...the earlier mutations..., this one]}`,
  where this one is `{"operators": [...], "why": "...", "defeats": "<the strategy that passed>"}`.

# Check before you finish

    sde-world check $output_dir       # build; repro fails twice, then passes twice after the fix

Also run your check that `target_fix` fails on the new world, and keep its output in
your notes. Finish only when `sde-world check` exits 0 and the old strategy fails.
