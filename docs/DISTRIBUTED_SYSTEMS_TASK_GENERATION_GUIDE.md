# Distributed Systems Task Generation Guide

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
