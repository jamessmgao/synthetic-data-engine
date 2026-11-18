"""
Diversity: what each candidate is drafted from.

- v1 (`sample_spec`): a taxonomy slot and difficulty profile (docs/PRD.md §6.2, §6.9;
  docs/TASK_GENERATION_RULES.md), the `spec` one-shot `render_initial_prompt` reads.
- v2 (`sample_spec_v2`, docs/AGENTIC_AUTHORING_PLAN.md §3.1-3.2): a problem family,
  mechanism, shape, environment, verification method and dial, optionally limited to
  an `area` (one or more families) and finer filters. A v2 spec also carries the v1
  {taxonomy, difficulty} view, so it works in either author mode.
"""

import random
from collections import Counter
from dataclasses import dataclass
from itertools import product

TAXONOMY: dict[str, list[str]] = {
    "domain": [
        "distributed systems", "databases", "networking", "security",
        "ML infrastructure", "data pipelines", "Linux tooling",
    ],
    "task_type": ["debugging", "implementation", "migration", "optimization", "investigation"],
    "core_capability": ["concurrency", "consistency", "resource management", "performance"],
    "failure_mode": [
        "race condition", "crash", "timeout", "resource exhaustion", "data corruption",
    ],
    # Each has a pinned base image in config/base_images.json.
    "environment": [
        "Python service", "Go service", "C program", "Java application",
        "Linux container (shell and system tools)", "PostgreSQL database",
    ],
    "skill_interaction": [
        "single skill", "two dependent skills", "multiple interacting constraints",
    ],
}

# No two tasks in a batch share this combination (PRD §6.9).
UNIQUE_DIMENSIONS = ("domain", "task_type", "environment")

# The v1 default (PRD §6.2), to be tuned once pilots show where glm-5.3-flash lands.
# A task FORM and a difficulty DIAL within it (generator/prompting.py FORMS), not
# generic counts: task-builder's pilots found easy/medium/hard labels and file or
# constraint counts didn't move the pass rate, while the coupled behavior-preserving
# change (a local fix that silently breaks a dependent module) gave the only genuine
# model failure (docs/ANALYSIS.md §9). Dial 1-3 turns its difficulty.
DEFAULT_DIFFICULTY = {"form": "behavior_preserving", "dial": 2}


def sample_spec(existing: list[dict], rng: random.Random | None = None) -> dict:
    """A spec for one new task, spread across the taxonomy given every task so far.

    Quota sampling over all tasks ever created (a batch adds its own picks as it goes): the new
    task takes an unused (domain, task_type, environment) combination with the
    least-used values, and the least-used value on every other dimension. Once
    every combination has been used, repeats are allowed, still least-used first.
    `existing` is the taxonomies of the tasks already created.
    """
    rng = rng or random.Random()
    used = {dim: Counter() for dim in TAXONOMY}
    taken: set[tuple[str, ...]] = set()
    for tax in existing:
        for dim, value in tax.items():
            if dim in used:
                used[dim][value] += 1
        taken.add(tuple(tax.get(d) for d in UNIQUE_DIMENSIONS))
    combos = list(product(*(TAXONOMY[d] for d in UNIQUE_DIMENSIONS)))
    combos = [c for c in combos if c not in taken] or combos
    combo = min(combos, key=lambda c: (sum(used[d][v] for d, v in zip(UNIQUE_DIMENSIONS, c)), rng.random()))
    taxonomy = dict(zip(UNIQUE_DIMENSIONS, combo))
    for dim, values in TAXONOMY.items():
        if dim not in taxonomy:
            taxonomy[dim] = min(values, key=lambda v: (used[dim][v], rng.random()))
    return {"taxonomy": taxonomy, "difficulty": DEFAULT_DIFFICULTY}


# --- taxonomy v2: problem families (docs/AGENTIC_AUTHORING_PLAN.md §3.1-3.2) ------------
#
# A task is a family (the kind of system), a mechanism (what makes it hard), a
# shape (fix / implement / optimize / migrate), an environment and a verification
# method, plus a dial 1-3. Families come from docs/HARD_TASK_GENERATION_GUIDE.md.


@dataclass(frozen=True)
class Family:
    label: str
    mechanisms: tuple[str, ...]
    environments: tuple[str, ...]
    verification: tuple[str, ...]


FAMILIES: dict[str, Family] = {
    "distributed_concurrency": Family(
        "Distributed systems and concurrency",
        ("adversarial_schedule", "partial_failure_recovery", "lost_acknowledged_write",
         "linearizability_violation", "snapshot_dedup_skew", "fencing_check_granularity", "lease_skew_direction",
         "session_index_without_term", "reconfig_quorum_early_switch", "tenant_key_dropped_on_replay",
         "dedup_watermark_from_truncated_suffix"),
        ("Python service", "Go service"),
        ("deterministic_scheduler", "fault_injection", "model_check_small")),
    "algorithmic_constraints": Family(
        "Algorithms under interacting constraints",
        # greedy_counterexample and optimality_required retired: greedy vs optimal names its
        # remedy (4 leaks on 2026-11-18); these run an exact algorithm with a defect inside it.
        ("global_constraint_interaction", "incremental_dp_stale_predecessor", "bound_inadmissible_with_group_cost",
         "shared_capacity_across_stages", "interval_tree_rotation_maxend_order",
         "tie_break_lost_on_incremental_update", "unsound_dominance_pruning",
         "residual_not_cancelled_on_capacity_decrease", "local_tiebreak_not_global_lexicographic",
         "state_key_omits_rare_component", "wraparound_boundary_in_sweep", "rollback_order_with_path_compression"),
        ("Python service", "Go service", "C program"),
        ("reference_solver", "exhaustive_small", "property_based")),
    "performance_resources": Family(
        "Performance and resource limits",
        # scalability_wall and memory_bound retired (generic: tonight's 5/5s and leaks).
        ("exact_semantics_under_speedup", "late_reference_retention", "coarse_invalidation_recompute",
         "lsm_compaction_pathology", "cross_boundary_quadratic", "offset_pagination_rescan",
         "stampede_on_shared_miss", "hot_key_shard_skew", "defensive_copy_churn", "unbounded_queue_collapse",
         "group_commit_durability", "adversarial_hash_degradation", "lock_convoy_across_io",
         "backtracking_in_tokenizer", "stable_tiebreak_parallel_merge", "reassociation_drift"),
        ("Python service", "Go service", "C program", "Java application"),
        ("benchmark_with_margin", "differential")),
    "database_consistency": Family(
        "Database consistency",
        ("isolation_anomaly", "write_skew", "crash_between_commit_and_effect", "idempotent_retry",
         "phantom_range_check", "mvcc_gc_vs_long_reader", "derived_state_rollback_drift", "session_read_your_writes"),
        ("Python service (SQLite)", "PostgreSQL database"),
        ("fault_injection", "invariant_check", "concurrent_integration")),
    "compilers_parsers": Family(
        "Compilers, interpreters and parsers",
        ("semantics_changing_transform", "aliasing_side_effects", "stateful_incremental_parse",
         "fragmented_frames", "error_recovery_state_leak", "constant_fold_semantics_mismatch", "macro_hygiene_capture"),
        ("Python service", "Go service"),
        ("differential_execution", "grammar_fuzzing", "reference_interpreter")),
    "cross_component_stateful": Family(
        "Stateful cross-component systems",
        ("multi_step_causal_failure", "retry_cache_commit_interaction",
         "compensation_after_irreversible_effect", "misleading_symptom", "cache_fill_race_divergence",
         "protocol_wrong_state_accept", "saga_timeout_double_compensation", "wall_clock_ordering_skew"),
        ("Python service", "Go service"),
        ("state_machine_test", "fault_injection", "integration")),
}

# --- application domains: WHAT system a world is (the user's package, 2026-11-18 23:30) --------
# A world was a ledger by default (about a third of the pool); now every new world and every
# variant gets a domain, sampled least-used first, and taxonomy.domain is that domain (not the
# family label), so uniqueness, the scenario dedup and the domain cap see the real system.
APP_DOMAINS: dict[str, str] = {
    "collab_editor": "collaborative text editor (CRDT/OT document sync)",
    "ride_dispatch": "ride / courier dispatch service",
    "package_resolver": "package dependency resolver and lockfile manager",
    "search_autocomplete": "search index with autocomplete",
    "rate_limiter": "API rate limiter / quota gateway",
    "file_sync": "file sync client (local and remote replicas)",
    "feature_flags": "feature-flag rollout service",
    "warehouse_inventory": "warehouse inventory and order picking system",
    "chat_presence": "chat presence and message fan-out service",
    "cdn_invalidation": "CDN cache with invalidation",
    "rbac_auth": "auth / permissions service (RBAC, group inheritance)",
    "build_cache": "build system with an incremental compile cache",
    "job_queue": "job queue and cron scheduler",
    "iot_telemetry": "IoT telemetry ingestion and aggregation",
    "video_pipeline": "video transcoding pipeline",
    "ad_bidding": "ad auction / real-time bidding service",
    "calendar": "calendar and meeting scheduler",
    "matchmaking": "game matchmaking service",
    "dns_cache": "DNS resolver with a cache",
    "log_shipping": "log shipping agent with batching and acknowledgements",
    "recommendation_feed": "recommendation / social feed builder",
    "email_delivery": "email delivery service with retries and bounces",
    "booking": "booking / reservation system",
    "map_tiles": "map tile renderer and tile cache",
    "metrics_tsdb": "metrics time-series database (downsampling, retention)",
    "payment_ledger": "payment ledger",
    "shopping_cart": "shopping cart and checkout service",
    "url_shortener": "URL shortener with click analytics",
    "config_service": "distributed configuration service with watches",
    "leaderboard": "game leaderboard with seasons",
    "parking_garage": "parking garage occupancy and billing system",
    "library_loans": "library loans and holds system",
    "hospital_beds": "hospital bed and ward assignment system",
    "airline_seats": "airline seat map and check-in service",
    "stock_orderbook": "stock exchange order book / matching engine",
    "school_timetable": "school timetable and room allocation",
    "notification_digest": "notification batching and digest service",
    "web_crawler": "web crawler frontier and politeness scheduler",
    "kv_store": "replicated key-value store",
    "elevator_control": "elevator group controller",
}
# Language-processing systems for the compilers family (a ride-dispatch "compiler" would be odd).
COMPILER_DOMAINS: dict[str, str] = {
    "spreadsheet_formulas": "spreadsheet formula engine with incremental recalculation",
    "query_language": "query language for a small document store",
    "template_engine": "HTML template engine",
    "config_language": "configuration language with includes and overrides",
    "regex_engine": "regular-expression engine",
    "build_dsl": "build-file DSL interpreter",
    "shader_minifier": "source-to-source minifier / optimizer",
    "markdown_renderer": "Markdown-to-HTML renderer",
    "bytecode_vm": "bytecode VM for a small scripting language",
    "sql_planner": "SQL parser and query planner",
}
ALL_DOMAINS: dict[str, str] = {**APP_DOMAINS, **COMPILER_DOMAINS}


# Per mechanism, the defect pattern as the world author plants it (world.md / world_variant.md
# render it as "Mechanism: <name>: <note>"). Each one's honest guarantee doesn't name its fix:
# the mechanisms whose did (greedy vs optimal: 4 leaks on 2026-11-18) are retired.
MECHANISM_NOTES: dict[str, str] = {
    # distributed_concurrency (L2): every textbook piece present and right on the happy path; the
    # defect is an interaction on a failure path, never a missing piece.
    "snapshot_dedup_skew": "the snapshot captures state at log index N but the dedup table at an earlier M; replay "
                           "from N+1 misses requests committed between M and N, so a retry after a restart applies twice",
    "fencing_check_granularity": "epochs/fencing tokens exist but are checked at the wrong point (on arrival, or once "
                                 "per batch flush), so a deposed leader's in-flight write lands after the new leader's",
    "lease_skew_direction": "lease expiry is measured from receipt by the holder but from send by the server; with "
                            "bounded delay and clock drift both act as holder for a window (state only the bounds)",
    "session_index_without_term": "read-your-writes compares a session's last write index with a follower's applied "
                                  "index but ignores the term, so after a truncated divergent suffix a follower serves stale data",
    "reconfig_quorum_early_switch": "during a membership change the commit quorum switches to the new configuration when "
                                    "the change is appended, not committed; a failover to an old-config node loses an acked entry",
    "tenant_key_dropped_on_replay": "the idempotency cache is keyed (tenant, request id) on the write path but rebuilt from "
                                    "the log by request id only, so after a failover one tenant gets another's cached response",
    "dedup_watermark_from_truncated_suffix": "a per-client dedup watermark is rebuilt from a log suffix later truncated, "
                                             "so the client's retry is dropped as a duplicate yet answered ok: an acked write lost",
    # algorithmic_constraints (L2): an exact algorithm already runs; the defect is inside it.
    "incremental_dp_stale_predecessor": "an exact DP's incremental add/remove path refreshes the memo forward but not the "
                                        "predecessor table of earlier items, so after some updates the result differs from a fresh plan",
    "bound_inadmissible_with_group_cost": "an exact branch-and-bound's relaxation bound mis-charges a shared group cost, "
                                          "so it underestimates and prunes the optimum on inputs where a group straddles the cut",
    "shared_capacity_across_stages": "a multi-stage max-flow model gives a resource shared by two stages a full capacity "
                                     "edge in each, so the plan over-allocates it when both stages draw from it",
    "interval_tree_rotation_maxend_order": "an augmented balanced tree updates a rotated parent's max before its child's, "
                                           "so after a double rotation a query skips a subtree that holds a match",
    "tie_break_lost_on_incremental_update": "an exact optimizer's stated deterministic tie-break holds on a fresh plan but not "
                                            "after incremental updates, which reconstruct an equally optimal different answer",
    # algorithmic_constraints (L5): sizes rule out brute force (the reference checks the counterexample);
    # the counterexample is a sequence or a structure, and a dependent relies on the API / output order.
    "unsound_dominance_pruning": "label-setting / DP prunes a state dominated on two resources but ignores a third that "
                                 "binds later (a cooldown, a budget); pruning is needed for the stated sizes",
    "residual_not_cancelled_on_capacity_decrease": "incremental max-flow / matching trims flow locally after a capacity "
                                                   "decrease instead of cancelling it in the residual graph; result infeasible or suboptimal",
    "local_tiebreak_not_global_lexicographic": "the lexicographically smallest optimal plan is required, but reconstruction "
                                               "picks the first optimal choice per state, locally smallest but not globally",
    "state_key_omits_rare_component": "the DP state / memo key omits a component that matters only when a rare rule fires "
                                      "(a precedence chain, a carried remainder), so two different states merge",
    "wraparound_boundary_in_sweep": "an exact sweep / DP over circular time linearizes at an arbitrary cut, so a solution "
                                    "crossing the cut is lost or double-counted",
    "rollback_order_with_path_compression": "undoable union-find combines path compression with rollback, so undo restores "
                                            "the wrong parents and later connectivity queries lie (an n log n bound is stated)",
    # database_consistency (L3)
    "phantom_range_check": "a range rule (no hour over 3 bookings) is guarded by locking the rows that already match, so "
                           "inserts into an empty part of the range slip through; locking matching rows is the wrong fix",
    "mvcc_gc_vs_long_reader": "cleanup drops versions older than now minus retention while a long export reads a snapshot, "
                              "so the export mixes two points in time; a longer retention is the wrong fix",
    "derived_state_rollback_drift": "a summary/counter table is updated outside the transaction or before a later failing "
                                    "step, so it drifts on aborts; recompute-after-success or periodic reconcile are wrong",
    "session_read_your_writes": "reads go to a lagging replica after the client's own write, while reads must keep working "
                                "when the primary is down (so all-reads-to-primary is the wrong fix)",
    # compilers_parsers (L3)
    "error_recovery_state_leak": "panic-mode error recovery leaves parser state (an open scope, a pending declaration) that "
                                 "changes the meaning of a later valid statement; every error must still be reported",
    "constant_fold_semantics_mismatch": "constant folding uses host arithmetic while the runtime has 32-bit wraparound, "
                                        "truncating division and float32, so results differ on edge values (*, <<, /, % too)",
    "macro_hygiene_capture": "a temporary introduced by a macro expansion captures a user name, but only across nested or "
                             "recursive expansions; a per-expansion gensym counter collides",
    # cross_component_stateful (L3)
    "cache_fill_race_divergence": "a read-miss cache fill races an update plus invalidate, so the cache stays stale forever; "
                                  "a TTL or delete-before-write is the wrong fix",
    "protocol_wrong_state_accept": "after a reconnect one component accepts a message only valid in a state the other reset "
                                   "(prepare, ack, commit); a check in the one handler the counterexample hits is not enough",
    "saga_timeout_double_compensation": "a step times out after succeeding; the orchestrator compensates and recovery replay "
                                        "compensates again; a per-order idempotency key blocks legitimate partial refunds",
    "wall_clock_ordering_skew": "events are ordered by local wall clocks, so a skewed node reorders causally related "
                                "writes and last-writer-wins drops a later update; clamping on the receiver only is wrong",
    # performance_resources (L4): the guarantee is an outcome (same output under N s / M MB), never the technique.
    "late_reference_retention": "a stream processor keeps every record in memory because a rare late correction may "
                                "reference any earlier id; a window or LRU drops a referenced record; guarantee: same "
                                "report, 6M records under M MB",
    "coarse_invalidation_recompute": "an incremental build/view invalidates a whole tenant or file per edit; per-item keys "
                                     "miss a transitive dependent, stale only after some edit orders; guarantee: equals a "
                                     "from-scratch run, each edit under N ms",
    "lsm_compaction_pathology": "compaction re-merges every run on each flush; a smarter merge drops tombstones early or "
                                "orders versions by run, not sequence, so deleted keys return; guarantee: same reads after "
                                "any put/delete/compact, fast",
    "cross_boundary_quadratic": "two linear components compose to O(n^2) across an API (a cursor that re-seeks per next(), "
                                "a per-element O(n) size()); caching it breaks snapshot reads under mutation; guarantee: "
                                "same output, N records in N s",
    "offset_pagination_rescan": "an export pages by offset through a repository, O(n^2); a keyset cursor on a non-unique "
                                "sort key skips or repeats tied rows; guarantee: every row exactly once under concurrent "
                                "writes, within N s",
    "stampede_on_shared_miss": "an expired key makes every concurrent request recompute behind a cache layer; naive "
                               "single-flight shares a failed result or merges keys across tenants; guarantee: K clients "
                               "each correct within N s",
    "hot_key_shard_skew": "one hot key serializes a sharded processor; splitting it across workers breaks per-key event "
                          "order, wrong only under some interleavings; guarantee: same per-key final state, N events in "
                          "N s with K workers",
    "defensive_copy_churn": "every getter deep-copies a large snapshot, so allocation and GC dominate; dropping the copy "
                            "aliases it and a caller's later mutation corrupts earlier readers; guarantee: same output "
                            "under a heap cap, in N s",
    "unbounded_queue_collapse": "a producer outruns its consumer with no backpressure; a bounded queue drops or deadlocks "
                                "in a cyclic stage that also produces; guarantee: every accepted message processed exactly "
                                "once within M MB",
    "group_commit_durability": "an fsync per record is too slow; batching acks before the batch's fsync loses acknowledged "
                               "writes on a kill mid-batch; guarantee: an acked write survives SIGKILL, W writes in N s",
    "adversarial_hash_degradation": "a custom hash table degrades with resize storms on the production key shape; the "
                                    "standard map changes iteration order the report depends on; guarantee: same report "
                                    "for production-shaped input in N s",
    "lock_convoy_across_io": "a global lock is held across I/O or a slow callback; narrowing it breaks atomicity of a "
                             "multi-field invariant; guarantee: K concurrent clients within N s, the cross-account total "
                             "always exact",
    "backtracking_in_tokenizer": "a tokenizer regex backtracks catastrophically on rare valid input; a rewritten pattern "
                                 "changes longest-match or escape handling on edge inputs; guarantee: every input parses "
                                 "identically, each in N ms",
    "stable_tiebreak_parallel_merge": "a chunked or parallel rewrite merges partial results and loses the tie order a "
                                      "downstream consumer depends on; guarantee: byte-identical output on every input, "
                                      "within N s",
    "reassociation_drift": "batching or vectorizing a float aggregation reorders additions, drifting past an "
                           "exact-to-the-cent contract on adversarial magnitudes; guarantee: totals match the reference "
                           "to the cent, in N s",
}


def describe(spec: dict) -> dict:
    """The spec's human text for the world author: domain_label and mechanism_note (the generator
    renders them as world.md's "Build a … system" and "Mechanism: …" lines)."""
    out = {}
    if spec.get("domain"):
        out["domain_label"] = ALL_DOMAINS.get(spec["domain"], spec["domain"].replace("_", " "))
    if MECHANISM_NOTES.get(spec.get("mechanism") or ""):
        out["mechanism_note"] = MECHANISM_NOTES[spec["mechanism"]]
    return out


def domains_for(family: str) -> tuple[str, ...]:
    """The domains a family's worlds are sampled from."""
    return tuple(COMPILER_DOMAINS) if family == "compilers_parsers" else tuple(APP_DOMAINS)


SHAPES = ("fix", "implement", "optimize", "migrate")
DIALS = (1, 2, 3)
DEFAULT_DIAL = 2

# Sampling weight per family: a family with weight 2 is picked about twice as often as
# one with weight 1. Equal for now; tune after the P1/P6 measurements.
FAMILY_WEIGHTS: dict[str, float] = {
    "distributed_concurrency": 1.0,
    "algorithmic_constraints": 1.0,
    "performance_resources": 1.5,  # the user's priority (2026-11-18 23:35): up modestly
    "database_consistency": 1.0,
    "compilers_parsers": 1.0,
    "cross_component_stateful": 1.0,
}

# The pinned base image (config/base_images.json) each environment is built on.
ENVIRONMENT_IMAGES: dict[str, str] = {
    "Python service": "python:3.12-slim-bookworm",
    "Python service (SQLite)": "python:3.12-slim-bookworm",  # sqlite3 ships with CPython
    "Go service": "golang:1.23-bookworm",
    "C program": "gcc:14-bookworm",
    "Java application": "eclipse-temurin:21-jdk-jammy",
    "PostgreSQL database": "postgres:16-bookworm",
}
# Diversity is spread over this combination (plan §3.1).
UNIQUE_V2 = ("family", "mechanism", "environment")

# One-shot mode's prompt (generator/prompting.py) reads {taxonomy, difficulty}; the
# v2 sampler fills those too, so a v2 spec works in either author mode.
_TASK_TYPE = {"fix": "Debugging", "implement": "Implementation", "optimize": "Optimization",
              "migrate": "Migration"}


def shape_for(mechanism: str) -> str:
    """The default shape a mechanism implies (plan §3.2)."""
    if mechanism == "optimality_required":
        return "implement"
    # Every performance mechanism is an optimize task (L4); so were the retired ones.
    if mechanism in ("scalability_wall", "memory_bound") or mechanism in FAMILIES["performance_resources"].mechanisms:
        return "optimize"
    if "schema" in mechanism:
        return "migrate"
    return "fix"


def families_listing() -> dict[str, dict]:
    """Every family with its allowed values, for GET /v1/families and 422 messages."""
    return {name: {"label": f.label, "weight": FAMILY_WEIGHTS.get(name, 1.0), "mechanisms": list(f.mechanisms),
                   "environments": list(f.environments), "verification": list(f.verification),
                   "shapes": sorted({shape_for(m) for m in f.mechanisms})}
            for name, f in FAMILIES.items()}


def resolve_area(area: str | list[str] | None) -> list[str]:
    """The families an `area` names (all of them if none). Raises ValueError naming
    the valid families for an unknown one."""
    if area is None:
        return list(FAMILIES)
    names = [area] if isinstance(area, str) else list(area)
    if not names:
        raise ValueError(f"area is empty; valid areas: {sorted(FAMILIES)}")
    unknown = [n for n in names if n not in FAMILIES]
    if unknown:
        raise ValueError(f"unknown area {unknown}; valid areas: {sorted(FAMILIES)}")
    return list(dict.fromkeys(names))


def legacy_view(spec: dict) -> dict:
    """The {taxonomy, difficulty} one-shot mode needs, derived from a v2 spec. taxonomy.domain is
    the spec's application domain (the family label for a spec from before domains)."""
    family = FAMILIES[spec["family"]]
    words = spec["mechanism"].replace("_", " ")
    return {
        "taxonomy": {"domain": spec.get("domain") or family.label, "task_type": _TASK_TYPE[spec["shape"]],
                     "core_capability": words, "failure_mode": words,
                     "environment": spec["environment"], "skill_interaction": "multiple interacting constraints"},
        "difficulty": {**DEFAULT_DIFFICULTY, "dial": spec["dial"]},
    }


def sample_spec_v2(existing: list[dict], area: str | list[str] | None = None, mechanism: str | None = None,
                   environment: str | None = None, dial: int | None = None,
                   rng: random.Random | None = None, domain: str | None = None) -> dict:
    """A spec v2 for one new task, within `area` and the optional filters.

    `existing` is the v2 specs of the tasks so far (a batch appends its own picks as it
    goes). The family with the least weighted use (uses / FAMILY_WEIGHTS) comes first;
    within it, an unused (mechanism, environment) combination with the least-used
    values, then the family's least-used verification method; ties broken by `rng`.
    A family repeats a combination only once all of its own have been used. The dial
    is DEFAULT_DIAL unless given. Raises ValueError for a filter no family in the area
    allows.
    """
    rng = rng or random.Random()
    families = resolve_area(area)
    combos = [(f, m, e) for f in families for m in FAMILIES[f].mechanisms for e in FAMILIES[f].environments
              if (mechanism is None or m == mechanism) and (environment is None or e == environment)]
    if not combos:
        raise ValueError(
            f"no family in area {families} allows "
            + ", ".join(f"{k}={v!r}" for k, v in (("mechanism", mechanism), ("environment", environment)) if v)
            + f"; allowed: {[families_listing()[f] for f in families]}")
    if dial is not None and dial not in DIALS:
        raise ValueError(f"dial must be one of {list(DIALS)}")

    used = {dim: Counter() for dim in (*UNIQUE_V2, "verification", "domain")}
    taken = set()
    for spec in existing:
        if "family" not in spec:
            continue  # a v1 or raw-prompt task
        for dim in used:
            if dim in spec:
                used[dim][spec[dim]] += 1
        taken.add(tuple(spec.get(d) for d in UNIQUE_V2))
    # The family first, by weighted use, so families stay balanced (or follow their
    # weights) however many combinations each has; then, within it, an unused
    # (mechanism, environment) with the least-used values.
    eligible = sorted({c[0] for c in combos})
    family = min(eligible, key=lambda f: (used["family"][f] / FAMILY_WEIGHTS.get(f, 1.0), rng.random()))
    in_family = [c for c in combos if c[0] == family]
    fresh = [c for c in in_family if c not in taken] or in_family
    _, mech, env = min(fresh, key=lambda c: (used["mechanism"][c[1]] + used["environment"][c[2]], rng.random()))
    # The domain: given (a variant keeps its world's), else the family's least-used one.
    if domain is None:
        domain = min(domains_for(family), key=lambda d: (used["domain"][d], rng.random()))
    spec = {
        "family": family, "mechanism": mech, "shape": shape_for(mech), "environment": env,
        "verification": min(FAMILIES[family].verification, key=lambda v: (used["verification"][v], rng.random())),
        "dial": dial if dial is not None else DEFAULT_DIAL, "domain": domain,
    }
    return {**spec, **describe(spec), **legacy_view(spec)}
