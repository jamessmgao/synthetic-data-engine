You are evolving a world that produced a task which was too easy. Five independent
agent runs solved it; a reviewer read their traces and found the strategy they used.
Your job is to change the WORLD itself, so that exactly that strategy no longer works,
while the world stays realistic, solvable and verifiable.

You work as root in a Linux container with a shell, in `/work` (scratch space).
Docker is available through the host's socket: label everything you start by hand
with `--label "$SDE_DOCKER_LABEL"` so it is cleaned up.

# The parent world

The world to evolve is in `/input/parent/` (read-only):

- `world.json`: its spec, invariants, counterexample, plausible wrong fix and lineage;
- `environment/`: the system, with its Dockerfile;
- `repro/repro.sh`: reproduces the counterexample (exit 1 on the world, 0 once fixed);
- `world_fix/fix.sh`: makes every invariant hold.

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

# Why it was too easy

- Why it was easy: the race is visible in one function, and a lock fixes it
- The strategy that worked: add a threading.Lock around reserve()
- The fix the agents applied: a process-local lock in reserve()
- Cue: "reservations are checked and written in reserve()" (instruction)

The strategy that passed (it must FAIL on the world you build):

a threading.Lock around reserve() (a single-process fix)

# The mutation to make

- **add_persistent_state**: Make the relevant state survive restarts, so a process-local or in-memory fix fails after a restart and recovery/replay must be handled.
- **extend_cross_component_dependency**: Move the root cause so the symptom shows in a different component from the cause, so patching where the symptom shows is not enough.

Why these: every passing run fixed it with an in-process lock in reserve()

Earlier mutations of this world (keep their effects; do not undo them):

- (none recorded)

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

- interacting_requirements: Make correctness guarantees depend on one another, so the straightforward fix for one violates another and the trade-off has to be understood.
- counterfactual_failure: Make a reasonable-looking implementation fail when events happen differently (another order, a retry, a crash, a concurrent caller), not only on the normal path.
- state_and_history: Make correctness depend on state over time: transitions, delayed effects, retries, recovery, changing conditions. What is right in one state is wrong in another.
- partial_observability: Spread the evidence for the root cause across logs, components or execution paths, so it has to be investigated and reconciled; everything needed stays available.
- nonlocal_cause: Make the symptom appear in a different component from the root cause, so dependencies and data flow have to be traced instead of patching where the symptom shows.
- adversarial_edge_cases: Add realistic failure sequences, unusual inputs, concurrent operations or boundary conditions that defeat the naive solution by exposing a real design weakness.
- locally_correct_globally_wrong: Make a greedy decision, isolated fix or simple heuristic look right but violate a stated overall requirement.
- dependent_subproblems: Require several connected decisions where earlier choices constrain later ones, so a partial solution is not enough.
- preserve_guarantees: Require the change without regressions: other behavior, including what hidden dependents rely on, must keep working (tested by a regression suite).
- genuine_tradeoff: Require balancing correctness with progress, concurrency, efficiency or reliability, without a trivial solution that disables one of them.

# What you produce

Write the new world to `/logs/artifacts/world/`, in the same layout as the parent:

- `environment/`, `repro/repro.sh`, `world_fix/fix.sh`;
- `world.json` with: the parent's spec; the invariants (updated if the mutation adds
  one); the NEW counterexample (sequence, `"reproduce": "repro/repro.sh"`, what was
  observed); a NEW `plausible_wrong_fix` (the strategy that passed is a good candidate,
  if it is now wrong for a real reason); and `lineage` =
  `{"parent_world_sha256": "0000000000000000000000000000000000000000000000000000000000000000", "mutations": [...the earlier mutations..., this one]}`,
  where this one is `{"operators": [...], "why": "...", "defeats": "<the strategy that passed>"}`.

# Check before you finish

    sde-world check /logs/artifacts/world       # build; repro fails twice, then passes twice after the fix

Also run your check that `target_fix` fails on the new world, and keep its output in
your notes. Finish only when `sde-world check` exits 0 and the old strategy fails.
