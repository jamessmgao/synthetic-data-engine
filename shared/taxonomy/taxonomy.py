"""
Diversity: the taxonomy slot and difficulty profile each candidate is drafted from
(docs/PRD.md §6.2, §6.9; docs/TASK_GENERATION_RULES.md).

The result is the `spec` that task-builder's `render_initial_prompt` reads.
"""

import random
from collections import Counter
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
DEFAULT_DIFFICULTY = {
    "level": "medium-hard",
    "relevant_files": "10-25",
    "constraints": 3,
    "subgoals": 2,
    "failure_scenarios": 1,
    "notes": [
        "The instruction describes symptoms, not the root cause.",
        "At least two constraints interact: the obvious fix for one breaks another.",
        "Two components are involved in the fix.",
    ],
}


def sample_specs(batch_id: str, count: int) -> list[dict]:
    """`count` specs for one batch, spread across the taxonomy.

    Quota sampling: each candidate takes the least-used values so far, ties
    broken by an RNG seeded with the batch id, so resubmitting a batch samples
    exactly the same specs.
    """
    rng = random.Random(batch_id)
    used = {dim: Counter() for dim in TAXONOMY}
    taken: set[tuple[str, ...]] = set()
    specs = []
    for _ in range(count):
        combos = [c for c in product(*(TAXONOMY[d] for d in UNIQUE_DIMENSIONS)) if c not in taken]
        if not combos:
            raise ValueError(f"taxonomy has no unused {UNIQUE_DIMENSIONS} combination left")
        combo = min(
            combos,
            key=lambda c: (sum(used[d][v] for d, v in zip(UNIQUE_DIMENSIONS, c)), rng.random()),
        )
        taken.add(combo)
        taxonomy = dict(zip(UNIQUE_DIMENSIONS, combo))
        for dim, values in TAXONOMY.items():
            if dim not in taxonomy:
                taxonomy[dim] = min(values, key=lambda v: (used[dim][v], rng.random()))
        for dim, value in taxonomy.items():
            used[dim][value] += 1
        specs.append({"taxonomy": taxonomy, "difficulty": DEFAULT_DIFFICULTY})
    return specs
