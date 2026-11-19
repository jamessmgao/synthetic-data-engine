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
        "lever": "a realistic execution sequence that defeats a seemingly correct protocol: at least 3 node "
                 "processes, and the defect needs two or more interleaved events (a second failover during "
                 "recovery, a duplicated message across a leader change), so no single straight-line "
                 "sequence shows it; the loss surfaces on a different node or component than its cause. "
                 "Every textbook piece is already there and works on the happy path (the WAL and its fsync, "
                 "quorum acks, request-ID dedup, the commit index, snapshots): the defect is how two of them "
                 "interact on the failure path, never a missing piece such as a commit() that is never "
                 "called. Patterns: a snapshot whose balances and dedup table are cut at different log "
                 "indexes; a fencing token checked per batch or at request entry, not at the write; lease "
                 "expiry measured from receipt on one side and from send on the other; a read-your-writes "
                 "index compared without its term after a truncation; a commit quorum taken from a "
                 "membership change that is appended but not committed; an idempotency key that keeps its "
                 "tenant on the write path but drops it on replay; a dedup watermark rebuilt from a suffix "
                 "that is later truncated; a balance applied by one path and skipped by the other. The system "
                 "also reports derived state per node (balances, dedup entries, roles, commit indexes) that "
                 "the fix must keep exact",
        "world": "e.g. a small replicated log or key-value store with a leader, followers, acknowledgements, "
                 "snapshots and failover (and membership changes); a multi-tenant request service with "
                 "idempotency keys and follower reads; a job queue with leases, retries and at-least-once "
                 "delivery; a lock or lease service with clocks, bounded message delay and expiry",
        "counterexample": "drive the system with a DETERMINISTIC scheduler or fault injector (an explicit "
                          "interleaving, a crash at a named point, a dropped or duplicated message) so the "
                          "violating schedule replays identically every time",
        "scaffold": "sde_sched.py for the interleaving (explore seeds, replay the schedule) and sde_fault.py for "
                    "crashes at named points and dropped or duplicated messages",
        "dependent": "the fix spans the writer and the recovery path (or the follower's apply loop), or a "
                     "compaction, snapshot or metrics module reads the log in the shape the bug produces",
    },
    "algorithmic_constraints": {
        "title": "Algorithmic and constraint-solving problems",
        "mechanisms": "global optimization, combinatorial interactions, invariants, algorithms that work "
                      "locally but fail globally",
        "lever": "the system already runs an exact algorithm (a DP, branch-and-bound, max-flow or matching) that "
                 "is right on its textbook core, and the defect is a subtle error inside or around it: a tie-break "
                 "that breaks a stated order, a constraint interaction the recurrence or pruning bound misses (a "
                 "bonus, a precedence, a capacity shared across stages), memoized state left stale by an "
                 "incremental update, or a bound that is not admissible on one input shape. Never a greedy or "
                 "heuristic where an optimal algorithm is needed: then \"the result is optimal\" names the fix. "
                 "Input sizes are stated where exhaustive search cannot finish in the time limit, so the fix "
                 "repairs the algorithm in place, not brute force. The defect must hide from random testing: "
                 "it shows only on a specific structure or history (an edit sequence, ties across two stages, "
                 "a solution straddling a boundary, a rule that fires late), so fuzzing small random inputs "
                 "against a brute-force reference passes, and finding it takes reasoning about the algorithm's "
                 "invariant, not a search for a failing input",
        "world": "e.g. a scheduler, allocator, matcher or planner that already plans with a DP, exact search or "
                 "flow, and also re-plans incrementally, caches sub-results or reports from the plan. A real "
                 "system, not a kernel: the algorithm is one module among several (parsing, persistence, a "
                 "report, an incremental path) that the defect's data flows through, so one read of a few "
                 "small files can't expose the design. The environment never ships a correct or reference "
                 "version of the algorithm (no --reference / --audit mode, no plan_reference(), no exact "
                 "fallback behind a flag): in the pass reviews of 2026-11-18 agents replaced the planner with "
                 "the shipped reference in one line",
        "counterexample": "a concrete input with the structure or history the defect needs, on which the exact "
                          "algorithm returns a wrong or suboptimal answer, checked against a brute-force reference "
                          "solver on that input. It needs a structure no small random input has: at least 5 "
                          "elements arranged a specific way (a tie across stages, an edit history, a window "
                          "straddling the boundary), never the 2-3 element case you find first (in the worlds of "
                          "2026-11-18, 3 of 5 had one, and a fuzzer finds it in seconds). Check it: run the shipped "
                          "and the fixed system on 200 seeded random inputs of at most 4 elements; they must agree "
                          "on every one (if one differs, rework the defect so it needs more structure), and record "
                          "the check in world.json as counterexample.random_small_agree (seed, count, result)",
        "scaffold": "sde_repro.py, with a small brute-force reference solver as the invariant check on the "
                    "counterexample input; the reference lives only in the world's check and the task's hidden "
                    "tests, never in the system the agent works on",
        "dependent": "a caller relies on the planner's current output shape or order (a report, a cache "
                     "keyed on it, an incremental re-plan), so rewriting the algorithm from scratch breaks it",
    },
    "performance_resources": {
        "title": "Performance and resource optimization",
        "mechanisms": "cost that only shows across a component boundary or under an adversarial but realistic "
                      "load shape (input, key distribution, concurrency, crash), where getting faster or "
                      "smaller must keep every result and guarantee the system already gives",
        "lever": "the obvious speedup (index, cache, batch, stream, parallelize, a finer lock, the standard "
                 "container) silently breaks a semantic the slow path gives for free: an order or tie, a late "
                 "reference, an acknowledged write's durability, a multi-field invariant, an exact total. The "
                 "requirement is stated as an outcome only: the same output (and guarantees) as now, within "
                 "N s / M MB on 1 CPU / 2 GB; never the structure, the tie-break or the edge case to keep",
        "world": "e.g. an export or pagination service, a telemetry rollup, a small LSM-style key-value store, an "
                 "incremental build or view, a tokenizer inside a parser, a sharded event processor, a "
                 "cache-fronted lookup service; not a ledger or payments system",
        "counterexample": "two inputs in repro/: a load shape where the current code misses the bound by 5x or "
                          "more, and one where the obvious fast version gives a different answer than the slow "
                          "reference (checked differentially); the plausible wrong fix is that fast version",
        "scaffold": "sde_repro.py, with the slow implementation as the differential reference and the bound "
                    "checked on the adversarial input",
        "dependent": "the cost and the semantic sit in different modules (a helper, a repository, a cache layer, "
                     "a consumer of the order), so a fast rewrite of the hot function alone breaks the other. "
                     "Too-easy worlds were 3 small files read in one step, the hot loop in plain sight: use 3+ "
                     "modules, the hot path crossing 2, and no name or comment that describes the cost (scan, "
                     "naive, linear, slow, find_all)",
    },
    "database_consistency": {
        "title": "Database consistency and recovery",
        "mechanisms": "phantoms over a range, write skew, snapshot lifetimes, derived state vs transaction "
                      "boundaries, replica lag, crashes between a commit and its effect",
        "lever": "a failure or concurrent execution that violates a business invariant even though every "
                 "individual database operation succeeds; never a plain lost update on one row: the DBA's "
                 "first fix (a conditional UPDATE, SELECT … FOR UPDATE, a UNIQUE constraint) is the plausible "
                 "wrong fix. Prefer a rule over a RANGE or a derived value: rows that may not exist yet, a "
                 "snapshot cleanup can tear, a summary written outside the transaction",
        "world": "e.g. a shift scheduler, enrollment or versioned config store on SQLite or Postgres, "
                 "with concurrent clients, long exports and a crash point; not a ledger or wallet",
        "counterexample": "clients run in a fixed interleaving (or a crash is injected at a named statement) "
                          "and the final state breaks the invariant",
        "scaffold": "sde_sched.py to interleave the clients' transactions step by step and sde_fault.py for a "
                    "crash between a commit and its side effect",
        "dependent": "the fix spans the write path and the retry/recovery worker, or a derived table or "
                     "report is built from rows in the shape the bug writes them",
    },
    "compilers_parsers": {
        "title": "Compilers, parsers, and semantic transformations",
        "mechanisms": "preserving exact behavior across complex inputs and interacting language rules: error "
                      "recovery, constant folding vs runtime arithmetic, macro hygiene, effect analysis",
        "lever": "a transformation or parser that appears valid but changes semantics under a particular "
                 "combination of aliasing, control flow or parser state; at least two passes or components "
                 "could plausibly produce the symptom, and no command prints the transformed form (no "
                 "show/dump/explain mode) that would display the bad rewrite directly. Strong shapes: error recovery "
                 "that leaves a scope or pending declaration open for a later valid statement, a fold that "
                 "uses the host's arithmetic instead of the target's, a hygiene counter that collides across "
                 "nested expansions, a pass that treats a call as pure when a callee mutates two calls deep",
        "world": "e.g. a small expression language with an optimizer pass, a macro expander, a query "
                 "rewriter, a config/template engine, or a streaming protocol parser fed fragmented frames",
        "counterexample": "a specific program or byte stream whose output differs between the reference "
                          "interpreter (or spec) and the transformed/parsed result",
        "scaffold": "sde_repro.py, comparing the transformed or parsed result with the reference on the recorded "
                    "input",
        "dependent": "a later pass, a pretty-printer or a source map relies on the node shape or positions the "
                     "current pass produces, so fixing the pass alone breaks the stage after it",
    },
    "cross_component_stateful": {
        "title": "Cross-component debugging and stateful workflows",
        "mechanisms": "causal diagnosis, hidden runtime state, interactions across modules, preserving earlier "
                      "guarantees: cache/source races, protocol state across reconnects, saga timeouts, "
                      "clock skew",
        "lever": "an observed failure that emerges from a sequence of individually reasonable decisions "
                 "across components: a cache fill racing an update and its invalidate, a message accepted "
                 "in a state another component reset on reconnect, a step compensated after it actually "
                 "succeeded, events ordered by skewed wall clocks",
        "world": "e.g. a provisioning, ride-dispatch, CI pipeline or document-collaboration workflow across "
                 "an API, a queue, a cache and a store, with retries, compensation and an irreversible "
                 "external effect",
        "counterexample": "a scripted sequence of requests, retries and failures after which the end state is "
                          "wrong (lost data, double effect, stale value), although each component behaved "
                          "plausibly on its own",
        "scaffold": "sde_sched.py for the order of requests and retries across components and sde_fault.py for "
                    "the failure between steps",
        "dependent": "the fix spans the producer and the consumer (or the compensation step), or a cache, "
                     "report or reconciliation job reads state that the buggy sequence leaves behind",
    },
}


def brief_for(family: str | None) -> str:
    """The brief text for a family (a generic one if the family is unknown)."""
    b = FAMILY_BRIEFS.get(family or "")
    if b is None:
        return ("Family: any of the six priority families. Pick the one the spec's mechanism fits best, "
                "and build the world around an executable counterexample.")
    # Kept short: world.md states the scope, the fault rule and the scaffolds once.
    return (f"Family: {b['title']}.\n"
            f"- Where the difficulty comes from: {b['mechanisms']}.\n"
            f"- Strongest lever: {b['lever']}.\n"
            f"- Typical worlds: {b['world']}.\n"
            f"- The counterexample: {b['counterexample']}.\n"
            f"- Why a local patch may fail here: {b['dependent']}.")


def domain_line(spec: dict) -> str:
    """world.md's / world_variant.md's "Build a … system" line, from the spec's domain_label
    (shared/taxonomy/taxonomy.py's APP_DOMAINS); empty for a spec from before domains."""
    label = spec.get("domain_label") or (spec.get("domain") or "").replace("_", " ")
    if not label:
        return ""
    return (f"**The system is a {label}**; the defect lives in its own logic. No accounts, balances, "
            "payments or refunds unless that domain has them.")


def mechanism_line(spec: dict) -> str:
    """The "Mechanism: …" line, from the spec's mechanism_note (shared/taxonomy/taxonomy.py's MECHANISM_NOTES)."""
    mechanism, note = spec.get("mechanism"), spec.get("mechanism_note")
    if not mechanism:
        return ""
    return f"**Mechanism: {mechanism}.** {note}" if note else f"**Mechanism: {mechanism}.**"
