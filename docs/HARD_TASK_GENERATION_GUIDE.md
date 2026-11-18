# Hard Task Generation Guide

How to generate difficult, verifiable engineering problems: a prompt for
distributed systems problems, a taxonomy of problem families and their difficulty
mechanisms, and a table of difficult task families with ways to verify them.

## 1. Generating difficult distributed systems problems

Generate an original, challenging distributed systems engineering problem.

**The difficulty should come from subtle interactions between correctness guarantees, not arbitrary complexity.**

Design the problem around these principles:

1. **Interacting invariants:** Require multiple guarantees to hold simultaneously, such as idempotency, ordering, consistency, availability, concurrency, and crash recovery. A straightforward fix for one invariant should risk violating another.
2. **Nonlocal reasoning:** Make the root cause span multiple components, state transitions, or transaction boundaries. The solution should require understanding the system as a whole rather than patching one obvious function.
3. **Adversarial execution sequences:** Introduce realistic failure scenarios involving crashes, retries, message duplication, reordering, partial commits, or concurrent operations. Bugs should emerge under specific interleavings rather than simple happy-path execution.
4. **Subtle but consequential defects:** Create a plausible implementation that works in ordinary cases but violates a clearly defined invariant under particular conditions. Avoid trivial syntax bugs or missing checks.
5. **Meaningful tradeoffs:** Make naive solutions insufficient. For example, serializing all operations might prevent races but violate an explicit concurrency or progress requirement.

**Keep the problem solvable and objectively verifiable.** Clearly specify all required behavior, provide enough context to reason about correctness, and ensure a valid solution exists.

Avoid artificial difficulty from excessive files, large inputs, obscure APIs, undocumented requirements, or unreliable tests.

**The ideal problem is one where an engineer can produce a seemingly correct solution that passes ordinary tests, yet fails under a carefully constructed execution sequence because they overlooked an interaction between system invariants.**

## 2. Problem families and their difficulty mechanisms

| Problem family | Difficulty mechanisms |
|---|---|
| Distributed systems | Interacting invariants, temporal reasoning, fault recovery |
| Performance optimization | Complexity analysis, resource tradeoffs, semantic preservation |
| Algorithmic problems | Global reasoning, counterexamples, search-space complexity |
| Database systems | Transaction boundaries, concurrency, persistent state |
| Compiler engineering | Semantic equivalence, nonlocal dependencies, adversarial cases |
| Security | Adversarial reasoning, policy interactions, privilege boundaries |

## 3. Taxonomy of difficult problems

This taxonomy groups problems by engineering discipline and the underlying reasoning challenge, rather than by programming language or superficial task type.

| Category | Example difficult problem | Why it is difficult | Verification |
|---|---|---|---|
| 1. Distributed systems and consensus | Repair a replicated log that acknowledges writes before they are durably committed across a leader change | Partial failures, coordination, ordering, consistency, and recovery | TLA+, model checking, fault injection |
| 2. Concurrency and synchronization | Fix a lock-free queue that loses elements under rare thread interleavings | Races, memory ordering, linearizability, deadlocks | Model checking, concurrency stress tests |
| 3. Performance optimization | Optimize a query executor that is correct but becomes prohibitively slow on adversarial joins | Algorithmic complexity, memory/CPU tradeoffs, preserving semantics | Differential tests, benchmarks |
| 4. Algorithmic and combinatorial reasoning | Replace a scheduler that incorrectly rejects feasible resource assignments | Greedy traps, global constraints, combinatorial search | Z3/SMT, reference solver |
| 5. Database transactions and consistency | Repair a concurrent booking system that oversells inventory under snapshot isolation | Isolation levels, write skew, transactional correctness, contention | Database integration tests, fault injection |
| 6. Stateful workflows and recovery | Repair an order processor that mishandles cancellation after payment authorization | State history, irreversible effects, retries, compensation | State-machine testing, invariant checks |
| 7. Cross-component debugging | Diagnose intermittent data loss caused by an interaction between API retries, event publishing, and database commits | Nonlocal root causes, causal reasoning, multiple abstraction layers | Integration tests, controlled failure injection |
| 8. Security and authorization | Fix an authorization engine where inherited permissions accidentally override explicit denials | Adversarial reasoning, rule precedence, privilege boundaries | Policy solvers, property-based testing |
| 9. Compilers and language tooling | Fix an optimization pass that changes program behavior when aliasing and exceptions interact | Semantic equivalence, data flow, control flow, edge cases | Differential execution, compiler test suites |
| 10. Parsing and protocol implementations | Repair a streaming protocol parser that mishandles fragmented and overlapping frames | Stateful parsing, ambiguous boundaries, adversarial inputs | Grammar-based fuzzing, differential testing |
| 11. Storage engines and indexing | Fix a B-tree implementation that corrupts records after interrupted page splits | Structural invariants, durability, recovery, on-disk consistency | Crash testing, property-based testing |
| 12. Networking and protocol correctness | Repair a transport implementation that mishandles retransmissions after sequence-number wraparound | Temporal behavior, packet reordering, state machines | Packet simulation, model checking |
| 13. Memory management and systems programming | Fix a concurrent memory reclamation scheme that permits use-after-free | Object lifetime, ownership, concurrency, low-level invariants | Sanitizers, model checking, stress tests |
| 14. Resource management and scheduling | Fix a worker scheduler that prevents deadlocks but causes starvation under contention | Safety versus liveness, fairness, resource allocation | State-machine tests, bounded exploration |
| 15. Schema evolution and compatibility | Migrate an event format while old and new clients operate concurrently | Compatibility guarantees, historical data, transition states | Cross-version integration tests |
| 16. Numerical and scientific computing | Repair a numerical algorithm that becomes unstable for ill-conditioned matrices | Numerical precision, stability, conditioning, complexity | High-precision references, numerical property tests |
| 17. Streaming and incremental computation | Fix a stream processor that produces incorrect aggregates for late or retracted events | Event time, mutable state, incremental updates, ordering | Differential testing, event-sequence simulation |
| 18. Distributed caching and invalidation | Fix a cache where delayed invalidation messages restore stale values | Version ordering, causality, concurrent updates | State-machine tests, adversarial event schedules |

These are potentially difficult task families, not guarantees that an individual problem will challenge a particular model.

## 4. The most promising categories for hard task generation

If the goal is to generate tasks that are both challenging and objectively verifiable, I'd prioritize six families:

### 1. Distributed systems and concurrency

Difficulty comes from interacting guarantees, partial failures, race conditions, recovery, and temporal reasoning.

**Strongest lever:** Introduce a realistic execution sequence that defeats a seemingly correct protocol.

### 2. Algorithmic and constraint-solving problems

Difficulty comes from global optimization, combinatorial interactions, invariants, and algorithms that work locally but fail globally.

**Strongest lever:** Construct a counterexample to a plausible greedy or heuristic solution.

### 3. Performance and resource optimization

Difficulty comes from meeting performance or memory requirements without sacrificing correctness.

**Strongest lever:** Make the straightforward correct implementation fundamentally unable to meet a clearly defined scalability requirement.

### 4. Database consistency and recovery

Difficulty comes from isolation anomalies, transactional boundaries, crashes, concurrent updates, and persistent state.

**Strongest lever:** Introduce a failure or concurrent execution that violates a business invariant despite individually successful database operations.

### 5. Compilers, parsers, and semantic transformations

Difficulty comes from preserving exact behavior across complex inputs and interacting language rules.

**Strongest lever:** Create a transformation that appears valid but changes semantics under a particular combination of aliasing, control flow, or parser state.

### 6. Cross-component debugging and stateful workflows

Difficulty comes from causal diagnosis, hidden runtime state, interactions across modules, and preserving previous guarantees.

**Strongest lever:** Make the observed failure emerge from a sequence of individually reasonable decisions across components.
