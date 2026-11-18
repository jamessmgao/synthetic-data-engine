"""Per-family world briefs for the world authoring instruction (plan §3.2).

One brief per taxonomy-v2 family, from docs/HARD_TASK_GENERATION_GUIDE.md §2-4 (the
difficulty mechanisms and each family's strongest lever) and
docs/WORLD_CONSTRUCTION.md §4 (the domain worlds and their adversarial executions).
"""

FAMILY_BRIEFS: dict[str, dict[str, str]] = {
    "distributed_concurrency": {
        "title": "Distributed systems and concurrency",
        "mechanisms": "interacting guarantees (idempotency, ordering, durability, availability), partial "
                      "failures, races, recovery and temporal reasoning",
        "lever": "a realistic execution sequence that defeats a seemingly correct protocol",
        "world": "e.g. a small replicated log or key-value store with a leader, followers, acknowledgements "
                 "and failover; a job queue with leases, retries and at-least-once delivery; a lock or lease "
                 "service with clocks and expiry",
        "counterexample": "drive the system with a DETERMINISTIC scheduler or fault injector (an explicit "
                          "interleaving, a crash at a named point, a dropped or duplicated message) so the "
                          "violating schedule replays identically every time",
        "scaffold": "sde_sched.py for the interleaving (explore seeds, replay the schedule) and sde_fault.py for "
                    "crashes at named points and dropped or duplicated messages",
    },
    "algorithmic_constraints": {
        "title": "Algorithmic and constraint-solving problems",
        "mechanisms": "global optimization, combinatorial interactions, invariants, algorithms that work "
                      "locally but fail globally",
        "lever": "a counterexample to a plausible greedy or heuristic solution",
        "world": "e.g. a scheduler, allocator, matcher or planner whose current implementation is a greedy or "
                 "first-fit heuristic that accepts or rejects inputs wrongly",
        "counterexample": "a concrete small input on which the plausible algorithm returns a wrong or "
                          "suboptimal answer, checked against a brute-force or reference solver on that input",
        "scaffold": "sde_repro.py, with a small brute-force reference solver as the invariant check on the "
                    "counterexample input",
    },
    "performance_resources": {
        "title": "Performance and resource optimization",
        "mechanisms": "complexity analysis, memory/CPU tradeoffs, preserving exact semantics while getting faster",
        "lever": "a straightforward correct implementation that fundamentally cannot meet a clearly stated "
                 "scalability requirement",
        "world": "e.g. a query executor, log reconciler, diff or index builder that is correct but quadratic, "
                 "or holds everything in memory",
        "counterexample": "an input shape (adversarial but realistic) where the current code exceeds a stated "
                          "time or memory bound by a wide margin (5x or more on 1 CPU / 2 GB), while a correct "
                          "faster version exists; correctness is checked differentially against the slow one",
        "scaffold": "sde_repro.py, with the slow implementation as the differential reference and the bound "
                    "checked on the adversarial input",
    },
    "database_consistency": {
        "title": "Database consistency and recovery",
        "mechanisms": "isolation anomalies, transaction boundaries, crashes, concurrent updates, persistent state",
        "lever": "a failure or concurrent execution that violates a business invariant even though every "
                 "individual database operation succeeds",
        "world": "e.g. a booking, inventory, ledger or quota service on SQLite or Postgres, with concurrent "
                 "clients, retries and a crash point between a commit and its side effect",
        "counterexample": "two or more clients run in a fixed interleaving (or a crash is injected at a named "
                          "statement) and the final state breaks the invariant, e.g. oversold stock or a "
                          "double charge",
        "scaffold": "sde_sched.py to interleave the clients' transactions step by step and sde_fault.py for a "
                    "crash between a commit and its side effect",
    },
    "compilers_parsers": {
        "title": "Compilers, parsers, and semantic transformations",
        "mechanisms": "preserving exact behavior across complex inputs and interacting language rules",
        "lever": "a transformation or parser that appears valid but changes semantics under a particular "
                 "combination of aliasing, control flow or parser state",
        "world": "e.g. a small expression language with an optimizer pass, a config/template engine, or a "
                 "streaming protocol parser fed fragmented and overlapping frames",
        "counterexample": "a specific program or byte stream whose output differs between the reference "
                          "interpreter (or spec) and the transformed/parsed result",
        "scaffold": "sde_repro.py, comparing the transformed or parsed result with the reference on the recorded "
                    "input",
    },
    "cross_component_stateful": {
        "title": "Cross-component debugging and stateful workflows",
        "mechanisms": "causal diagnosis, hidden runtime state, interactions across modules, preserving earlier "
                      "guarantees",
        "lever": "an observed failure that emerges from a sequence of individually reasonable decisions "
                 "across components",
        "world": "e.g. an order, payment or provisioning workflow across an API, a queue, a cache and a store, "
                 "with retries, compensation and an irreversible external effect",
        "counterexample": "a scripted sequence of requests, retries and failures after which the end state is "
                          "wrong (lost data, double effect, stale value), although each component behaved "
                          "plausibly on its own",
        "scaffold": "sde_sched.py for the order of requests and retries across components and sde_fault.py for "
                    "the failure between steps",
    },
}


def brief_for(family: str | None) -> str:
    """The brief text for a family (a generic one if the family is unknown)."""
    b = FAMILY_BRIEFS.get(family or "")
    if b is None:
        return ("Family: any of the six priority families. Pick the one the spec's mechanism fits best, "
                "and build the world around an executable counterexample.")
    return (f"Family: {b['title']}.\n"
            f"- Where the difficulty comes from: {b['mechanisms']}.\n"
            f"- Strongest lever: {b['lever']}.\n"
            f"- Typical worlds: {b['world']}.\n"
            f"- The counterexample: {b['counterexample']}.\n"
            f"- Scaffolds (/opt/sde/scaffold): {b['scaffold']}.\n"
            "- Scope: about 300-800 lines in 2-4 modules; the difficulty is in how they interact, not in size.")
