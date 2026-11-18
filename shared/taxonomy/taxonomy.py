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


def sample_spec(existing: list[dict], rng: random.Random | None = None) -> dict:
    """A spec for one new task, spread across the taxonomy given every task so far.

    Quota sampling over all tasks ever created (there are no batches): the new
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
