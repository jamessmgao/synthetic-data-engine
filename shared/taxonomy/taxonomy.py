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
         "linearizability_violation"),
        ("Python service", "Go service"),
        ("deterministic_scheduler", "fault_injection", "model_check_small")),
    "algorithmic_constraints": Family(
        "Algorithms under interacting constraints",
        ("greedy_counterexample", "global_constraint_interaction", "optimality_required"),
        ("Python service", "Go service", "C program"),
        ("reference_solver", "exhaustive_small", "property_based")),
    "performance_resources": Family(
        "Performance and resource limits",
        ("scalability_wall", "memory_bound", "exact_semantics_under_speedup"),
        ("Python service", "Go service", "C program", "Java application"),
        ("benchmark_with_margin", "differential")),
    "database_consistency": Family(
        "Database consistency",
        ("isolation_anomaly", "write_skew", "crash_between_commit_and_effect", "idempotent_retry"),
        ("Python service (SQLite)", "PostgreSQL database"),
        ("fault_injection", "invariant_check", "concurrent_integration")),
    "compilers_parsers": Family(
        "Compilers, interpreters and parsers",
        ("semantics_changing_transform", "aliasing_side_effects", "stateful_incremental_parse",
         "fragmented_frames"),
        ("Python service", "Go service"),
        ("differential_execution", "grammar_fuzzing", "reference_interpreter")),
    "cross_component_stateful": Family(
        "Stateful cross-component systems",
        ("multi_step_causal_failure", "retry_cache_commit_interaction",
         "compensation_after_irreversible_effect", "misleading_symptom"),
        ("Python service", "Go service"),
        ("state_machine_test", "fault_injection", "integration")),
}

SHAPES = ("fix", "implement", "optimize", "migrate")
DIALS = (1, 2, 3)
DEFAULT_DIAL = 2

# Sampling weight per family: a family with weight 2 is picked about twice as often as
# one with weight 1. Equal for now; tune after the P1/P6 measurements.
FAMILY_WEIGHTS: dict[str, float] = {
    "distributed_concurrency": 1.0,
    "algorithmic_constraints": 1.0,
    "performance_resources": 1.0,
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
    if mechanism in ("scalability_wall", "memory_bound"):
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
    """The {taxonomy, difficulty} one-shot mode needs, derived from a v2 spec."""
    family = FAMILIES[spec["family"]]
    words = spec["mechanism"].replace("_", " ")
    return {
        "taxonomy": {"domain": family.label, "task_type": _TASK_TYPE[spec["shape"]],
                     "core_capability": words, "failure_mode": words,
                     "environment": spec["environment"], "skill_interaction": "multiple interacting constraints"},
        "difficulty": {**DEFAULT_DIFFICULTY, "dial": spec["dial"]},
    }


def sample_spec_v2(existing: list[dict], area: str | list[str] | None = None, mechanism: str | None = None,
                   environment: str | None = None, dial: int | None = None,
                   rng: random.Random | None = None) -> dict:
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

    used = {dim: Counter() for dim in (*UNIQUE_V2, "verification")}
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
    spec = {
        "family": family, "mechanism": mech, "shape": shape_for(mech), "environment": env,
        "verification": min(FAMILIES[family].verification, key=lambda v: (used["verification"][v], rng.random())),
        "dial": dial if dial is not None else DEFAULT_DIAL,
    }
    return {**spec, **legacy_view(spec)}
