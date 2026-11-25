# Difficulty: what makes a task hard, and how we aim for it

What "difficult" means for synthetic-data-engine, where difficulty comes from, the problem
families and their levers, what we measured against the pinned solver, and the design rules
that follow.

Merged on 2026-11-19 from `DIFFICULTY.md`, `DIFFICULTY.md` and
`DIFFICULTY.md`. Code cites §2–4 (the families) and §6 (the
implications); those numbers are kept.

Related:
- [AUTHORING.md](AUTHORING.md): how a task is built (worlds, mutations, variants);
- [WORLD_CONSTRUCTION.md](WORLD_CONSTRUCTION.md): the world method;
- [VALIDATION.md](VALIDATION.md): the gates;
- [HILLCLIMBING.md](HILLCLIMBING.md): how we tune it.

## 1. What "difficult" means

A task is **accepted** when the pinned solver (terminus-2 + glm-5.3-flash at
`reasoning_effort=high`, 1 h agent timeout) passes it **1 to 3 times out of 5** agent runs
(`N_RUNS=5`, `BAND_LOW=1`, `BAND_HIGH=3` in shared/contracts/config.py). The objective is **yield**:
the share of finished tasks that land in that band.

- 4/5 or 5/5 is too easy: a world mutation makes it harder.
- 0/5 is too hard: an easier revision, or a repair if the Trace Parser blames the task.
- In band for the wrong reason doesn't count. INVALID, cancelled and skipped agent runs aren't
  failures, and a timeout is a failure only when the Trace Parser rules it the model's own
  fault.

So "difficult" means **a capable agent can solve it, but often doesn't**, because finding or
correctly fixing the problem takes real investigation, not because the task is broken or vague.

### Where difficulty comes from

| Source | Why LLM agents struggle | Example |
|---|---|---|
| Combinatorial search | too many candidate solutions | an optimal schedule under 15 interacting constraints |
| Long-horizon planning | early mistakes propagate | a 30-step deployment workflow |
| Constraint interaction | satisfying one requirement violates another | change code without changing existing behavior |
| State tracking | an accurate picture of changing information must be kept | inventory across many transactions |
| Unfamiliar reasoning | memorized patterns stop helping | custom rules in a small DSL |
| Partial observability | needed information is hidden or spread out | debugging from incomplete logs |
| Error detection and recovery | the model must notice its own mistake | diagnose, repair, retry safely |

Three dominate: **interacting constraints** (a model satisfies four and quietly violates the
fifth), **large search spaces with weak guidance**, and **errors that compound** across
dependent steps. Verification and recovery counteract the last one, but only when the model
notices something went wrong.

**Harder doesn't mean longer.** What matters is the precision of reasoning required: a long,
repetitive task can be easy; changing one function without breaking a hidden dependent can be
very hard.

**Regimes.**
- **Easy:** a familiar strategy works.
- **Frontier (the target):** the model has the capabilities but must apply them consistently
  across several interacting decisions.
- **Beyond capability:** a capability, a crucial fact, or a way to search or verify is missing.

Tools and time are part of difficulty: an agent with a shell and an hour writes a brute-force
or ILP solver, so "large search space" alone is weak.

**A working model, D = f(B, C, H, U, V):**
- **B** branching with no cheap tool shortcut;
- **C** constraint coupling;
- **H** horizon (dependent steps that must all be right);
- **U** unfamiliarity (custom rules, not a textbook bug);
- **V** verifiability, inverse: the agent's own tests pass while a hidden requirement fails.

The frontier is where everything is stated and solvable, but several of these are high at once.

## 2. Problem families and their difficulty mechanisms

| Problem family | Difficulty mechanisms |
|---|---|
| Distributed systems | interacting invariants, temporal reasoning, fault recovery |
| Performance optimization | complexity analysis, resource tradeoffs, semantic preservation |
| Algorithmic problems | global reasoning, counterexamples, search-space complexity |
| Database systems | transaction boundaries, concurrency, persistent state |
| Compiler engineering | semantic equivalence, nonlocal dependencies, adversarial cases |
| Security | adversarial reasoning, policy interactions, privilege boundaries |

The six families the pipeline generates, with their current mechanisms, are in
`shared/taxonomy/taxonomy.py` and [AUTHORING.md §3.2](AUTHORING.md).

## 3. Taxonomy of difficult problems

Grouped by engineering discipline and the underlying reasoning challenge, not by language or
task type. These are potentially difficult families, not guarantees for any one task.

| Category | Example difficult problem | Why it is difficult | Verification |
|---|---|---|---|
| 1. Distributed systems and consensus | Repair a replicated log that acknowledges writes before they are durably committed across a leader change | Partial failures, coordination, ordering, consistency, recovery | TLA+, model checking, fault injection |
| 2. Concurrency and synchronization | Fix a lock-free queue that loses elements under rare interleavings | Races, memory ordering, linearizability, deadlocks | Model checking, stress tests |
| 3. Performance optimization | Optimize a query executor that is correct but prohibitively slow on adversarial joins | Complexity, memory/CPU tradeoffs, preserving semantics | Differential tests, benchmarks |
| 4. Algorithmic and combinatorial reasoning | Replace a scheduler that rejects feasible resource assignments | Greedy traps, global constraints, search | Z3/SMT, reference solver |
| 5. Database transactions and consistency | Repair a booking system that oversells under snapshot isolation | Isolation levels, write skew, contention | Integration tests, fault injection |
| 6. Stateful workflows and recovery | Repair an order processor that mishandles cancellation after payment authorization | State history, irreversible effects, compensation | State-machine tests, invariant checks |
| 7. Cross-component debugging | Diagnose data loss from API retries, event publishing and commits interacting | Nonlocal root causes, causal reasoning | Integration tests, failure injection |
| 8. Security and authorization | Fix an engine where inherited permissions override explicit denials | Rule precedence, privilege boundaries | Policy solvers, property tests |
| 9. Compilers and language tooling | Fix an optimization pass that changes behavior when aliasing and exceptions interact | Semantic equivalence, data and control flow | Differential execution |
| 10. Parsing and protocols | Repair a streaming parser that mishandles fragmented, overlapping frames | Stateful parsing, ambiguous boundaries | Grammar fuzzing, differential tests |
| 11. Storage engines and indexing | Fix a B-tree that corrupts records after interrupted page splits | Structural invariants, durability, recovery | Crash testing, property tests |
| 12. Networking | Repair retransmission handling after sequence-number wraparound | Temporal behavior, reordering, state machines | Packet simulation, model checking |
| 13. Memory management | Fix a reclamation scheme that permits use-after-free | Object lifetime, ownership, concurrency | Sanitizers, model checking |
| 14. Resource management and scheduling | Fix a scheduler that avoids deadlock but starves under contention | Safety vs liveness, fairness | State-machine tests, bounded exploration |
| 15. Schema evolution | Migrate an event format while old and new clients run concurrently | Compatibility, transition states | Cross-version integration tests |
| 16. Numerical computing | Repair an algorithm that is unstable for ill-conditioned matrices | Precision, stability, conditioning | High-precision references |
| 17. Streaming and incremental computation | Fix a stream processor with wrong aggregates for late or retracted events | Event time, incremental state, ordering | Differential tests, event simulation |
| 18. Distributed caching | Fix a cache where delayed invalidations restore stale values | Version ordering, causality | State-machine tests, adversarial schedules |

## 4. The six priority families and their levers

Each family's brief in `generator/families.py` states its lever, the kind of world, the
counterexample, the scaffold and the dependent; every mechanism has a one-line defect pattern
in `shared/taxonomy/taxonomy.py` `MECHANISM_NOTES`.

1. **Distributed systems and concurrency.** Interacting guarantees, partial failures, races,
   recovery, temporal reasoning. **Lever:** a realistic execution sequence that defeats a
   seemingly correct protocol. Every textbook piece is present and right on the happy path; the
   defect is an interaction on a failure path, never a missing piece.
2. **Algorithmic and constraint-solving problems.** Global optimization, combinatorial
   interactions, algorithms right locally and wrong globally. **Lever (as built):** an exact
   algorithm (DP, branch-and-bound, flow, matching) correct at its textbook core, with a subtle
   defect that needs cross-part reasoning. Greedy-vs-optimal was retired: the honest guarantee
   ("optimal") names the fix (0 of 11 tasks accepted).
3. **Performance and resource optimization.** Meeting a time or memory bound without
   sacrificing correctness. **Lever (as built):**
   - the obvious speedup (index, cache, batch, stream, parallelize, a finer lock, the standard
     container) silently breaks a semantic the slow path gives for free;
   - the bound is stated as an outcome only, and it covers the reads too, so deferring the work
     doesn't escape it.
4. **Database consistency and recovery.** Isolation anomalies, transaction boundaries, crashes,
   persistent state. **Lever:** a failure or concurrent execution that violates a business
   invariant although every operation succeeds. Never a plain lost update: the DBA's first fix
   is the plausible wrong fix.
5. **Compilers, parsers and semantic transformations.** Exact behavior across complex inputs
   and interacting rules. **Lever:** a transformation that looks valid but changes semantics
   under a combination of aliasing, control flow or parser state, where 2+ components could
   produce the symptom and no command prints the transformed form.
6. **Cross-component debugging and stateful workflows.** Causal diagnosis, hidden state,
   preserved guarantees. **Lever:** a failure that emerges from individually reasonable
   decisions across components.

## 5. What we measured

Against terminus-2 + glm-5.3-flash (pilots p1–p4, whose records are in `experiments/results/`,
then the production runs of 2026-11-18/19 in [ITERATION_LOG.md](ITERATION_LOG.md)).

**It is very good at:**
- reading the code, finding a local bug and patching it (9/9 early debugging tasks solved
  4–5/5);
- textbook patterns, with or without hints;
- building and wiring systems;
- verifying and recovering: it writes its own differential tests and fuzzers, benchmarks, and
  fixes what it sees break.

**Why tasks came out too easy** (the Trace Parser's pass reviews):

| Cause | Example | Rule (§6) |
|---|---|---|
| A tiny codebase read in one command, diagnosed at step 3–4 | most algorithmic and performance worlds (2–5 files, ~4 KB) | 1 |
| The correct algorithm shipped in the environment | aws-w2-l2-t03: the whole fix was `return plan_reference(…)` | 2 |
| The guarantee names the fix ("optimal" vs a greedy rule) | greedy_counterexample 0/5 accepted, optimality_required 0/6 | 4 |
| The instruction or the program's output names the cost | "every flush … immediately calls compact()"; `clients=32 elapsed_ms=7066 bound_ms=1200` | 4 |
| Fail-loud bugs (a crash on first use) | aws-iter2-t11 v1: 5/5 | 3 |
| The work could be deferred instead of made faster | perf-t02 deferred compaction; perf-py rebuilt lazily on read; perf-t05 moved the fetch out of the lock (all 2026-11-19) | 5 |
| More files, constraints or subgoals | the one-shot level knobs added bugs of the same trivial kind | 1 |

**What looked hard but was a task defect:** unstated requirements (a hidden password, an exact
error payload, an HTTP 409 the instruction never mentions), a tool missing from `PATH`, tests
that outran the verifier's 900 s, a harness that raced its own SIGKILL. None of these count as
model failures; [AGENT_FAILURE_ANALYSIS.md](AGENT_FAILURE_ANALYSIS.md) has the cases.

**The authoring ceiling.** The harder the brief, the less often glm-5.1 authors it correctly:
6 of 8 hard one-shot briefs failed their own reference solution on the first try. Hard tasks
cost repairs; the author must be able to solve what it asks.

## 6. Implications: the design rules

1. **The bug lives in a real system, not a kernel.** The defect's data flows through several
   modules; no name or comment describes the cost or the limit (`limit_named`).
2. **Never ship the answer.** No reference, audit or exact-fallback path in the environment;
   brute-force references belong in the world check and the hidden tests only.
3. **The defect hides from random testing.** It needs a structure or history (an edit
   sequence, ties across two stages, a crash at one point, an interleaving), and it never fails
   loud on first use.
4. **The instruction states outcomes, never mechanisms.** It gives the symptom and the
   guarantee through the system's interface (75–130 user-voiced words worked best). It never
   names the fix, its location, the failing sequence or the exact cases. The honest guarantee
   itself must not name the fix; if it does, the mechanism is wrong (greedy vs optimal).
5. **The fix is not local, and the obvious fix is wrong.** The real fix spans 2+ files or a
   one-file patch breaks a dependent. The plausible wrong fix is the agent's natural first
   move: it passes the agent's first repro and fails the invariant. For performance, deferring
   the work is covered by the bound too.
6. **Couple the requirements (C) and aim at low verifiability (V).** The model recovers from
   any mistake it can see; it fails when its own checks pass but a stated requirement doesn't.
7. **Use unfamiliar rules (U), not trivia,** and keep search hard after tools: if a solver
   trivializes it, require exactness on instances where brute force is too slow, or move the
   difficulty to C/U/V.
8. **Give difficulty a dial** (1–3), and **never fake it:** no unstated requirements, no
   environment traps, no difficulty from running out the clock.
9. **Thorough hidden tests.** Check every stated outcome, including restarts, concurrency and
   the dependent. Start their own services on a port they pick, and finish well inside 900 s.
10. **Check failures by reading trajectories.** A task is in band only when its failures are
    the model's own reasoning errors.

**Checklist for a new mechanism, brief or prompt change.** The resulting tasks would have:

- [ ] a system of several modules the defect's data flows through (rule 1);
- [ ] no reference, correct or exact fallback in the environment (2);
- [ ] a defect that needs a structure or history, and doesn't fail loud (3);
- [ ] an honest guarantee that names neither the fix nor the textbook problem (4);
- [ ] a non-local fix, and a wrong fix that is the agent's natural first move (5);
- [ ] coupled requirements tested together, plus every stated edge case (6, 9);
- [ ] tests that start their own services and finish well inside 900 s (9).

Then measure it ([HILLCLIMBING.md](HILLCLIMBING.md)): one change per lever per window, judged
on the tasks authored after it.

## 7. A prompt for generating distributed systems problems

> Generate an original, challenging distributed systems engineering problem. The difficulty
> should come from subtle interactions between correctness guarantees, not arbitrary
> complexity.
>
> 1. **Interacting invariants:** several guarantees must hold at once (idempotency, ordering,
>    consistency, availability, concurrency, crash recovery); a straightforward fix for one
>    risks another.
> 2. **Nonlocal reasoning:** the root cause spans components, state transitions or
>    transaction boundaries.
> 3. **Adversarial execution sequences:** crashes, retries, duplication, reordering, partial
>    commits or concurrency; the bug emerges under specific interleavings.
> 4. **Subtle but consequential defects:** a plausible implementation that works in ordinary
>    cases and violates a clearly defined invariant under particular conditions.
> 5. **Meaningful tradeoffs:** naive solutions are insufficient (serializing everything
>    violates a stated progress requirement).
>
> Keep it solvable and objectively verifiable; avoid difficulty from excessive files, large
> inputs, obscure APIs, undocumented requirements or unreliable tests. The ideal problem is
> one where an engineer produces a seemingly correct solution that passes ordinary tests,
> yet fails under a carefully constructed execution sequence.
