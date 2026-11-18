"""
Difficulty decisions for one evaluated version (docs/PRD.md §6.6): the pinned
band, which revision operator to try next, and the evidence handed to GLM-5.1.

Operator names are task-builder's `prompting.OPERATORS`.
"""

import hashlib
import json
import re
from statistics import median
from typing import Any

from shared.contracts import config
from shared.contracts.models import Outcome


def eval_config() -> dict[str, Any]:
    """The pinned evaluation config every scored rollout of a version must share."""
    return {
        "agent": config.EVAL_AGENT,
        "model": config.EVAL_MODEL,
        "agent_kwargs": {"reasoning_effort": config.EVAL_REASONING_EFFORT},
        "n_runs": config.N_RUNS,
        "band": [config.BAND_LOW, config.BAND_HIGH],
    }


def config_hash() -> str:
    return hashlib.sha256(json.dumps(eval_config(), sort_keys=True).encode()).hexdigest()[:16]


def in_band(pass_count: int) -> bool:
    return config.BAND_LOW <= pass_count <= config.BAND_HIGH


def too_easy(pass_count: int) -> bool:
    return pass_count > config.BAND_HIGH


def too_hard(pass_count: int) -> bool:
    return pass_count < config.BAND_LOW

# Tried in order; an operator already used on this task is skipped.
HARDER = ["increase_diagnosis_depth", "increase_constraint_interaction", "increase_cross_component_dependency"]
EASIER = ["localize_diagnosis", "reduce_unrelated_constraints"]


def choose_operator(pass_count: int, rollouts: list[dict], used: list[str]) -> str:
    """Fixed rules over the evidence: too easy → harder, 0/5 → easier."""
    if pass_count == 0:
        order = list(EASIER)
    else:
        order = list(HARDER)
        # Passing runs that each touched one file at most: the fix is a local
        # patch, so make it span two modules first.
        touched = [
            len(files)
            for r in rollouts
            if r["outcome"] == Outcome.VALID_PASS
            and (files := (r.get("trace_metrics") or {}).get("files_modified")) is not None
        ]
        if touched and median(touched) <= 1:
            order.remove("increase_cross_component_dependency")
            order.insert(0, "increase_cross_component_dependency")
    fresh = [op for op in order if op not in used]
    return (fresh or order)[0]


def failing_tests(verifier_stdout: str) -> list[str]:
    """`FAILED path::test - message` lines from pytest's -rA summary."""
    return [line.removeprefix("FAILED ").strip() for line in verifier_stdout.splitlines() if line.startswith("FAILED ")]


FAILED_ASSERTION_LINES = 4  # per failed run, from the trace parser's evidence


def _cut(text: str) -> bool:
    """An unescaped quote ended the field early (trace_parser.is_cut): 'returns status '."""
    return bool(re.search(r"(?:\s|[=:(,])$", text))


def evidence(rollouts: list[dict]) -> str:
    """One line per scored run, for the revision prompt.

    Uses the trace parser's `failure` record: its conclusion and explanation (marked if it
    stops mid-sentence), the failing tests and the first assertion lines; else any
    `failing_tests` the caller attached."""
    lines = []
    for r in sorted(rollouts, key=lambda r: r["slot"]):
        metrics = r.get("trace_metrics") or {}
        seconds = metrics.get("duration_seconds") or (r.get("result") or {}).get("duration_sec")
        took = f", {seconds:.0f}s" if seconds else ""
        if r["outcome"] == Outcome.VALID_PASS:
            # How easily it passed is part of the evidence: steps and what it edited.
            steps = f", {metrics['llm_calls']} agent steps" if metrics.get("llm_calls") else ""
            files = metrics.get("files_modified")
            edited = f", edited {', '.join(files[:5])}" if files else ""
            lines.append(f"- run {r['slot']}: passed{took}{steps}{edited}")
            continue
        failure = r.get("failure") or {}
        tests = failure.get("failing_tests") or r.get("failing_tests") or []
        explanation = failure.get("explanation")
        if explanation and _cut(explanation):  # never hand the author a reason that stops mid-sentence
            explanation += " [the reviewer's explanation is cut off here]"
        why = " ".join(filter(None, [failure.get("conclusion"), explanation])) or "; ".join(tests) \
            or "no explanation recorded"
        lines.append(f"- run {r['slot']}: failed{took}: {why}")
        if tests and why != "; ".join(tests):
            lines.append(f"  failing tests: {'; '.join(tests)}")
        asserted = [e["text"] for e in failure.get("evidence") or [] if (e.get("text") or "").startswith("E ")]
        lines.extend(f"  {a}" for a in asserted[:FAILED_ASSERTION_LINES])
    return "\n".join(lines)



# World mutation operators (docs/AGENTIC_AUTHORING_PLAN.md §3.6, WORLD_CONSTRUCTION.md §8),
# plus remove_cues: rewrite the instruction or code sentences that point at the fix, at the
# interface level, with the world unchanged.
MUTATION_OPERATORS = [
    "add_concurrent_execution", "inject_partial_failure", "introduce_global_invariant",
    "generate_counterexample", "tighten_resource_bound", "add_persistent_state",
    "extend_cross_component_dependency", "add_semantic_edge_case", "add_realistic_distractor",
    "compose_failure_modes", "expand_state_space", "introduce_temporal_constraint", "remove_cues",
]
MAX_MUTATIONS = 2  # difficulty operators per mutation; remove_cues comes on top
# Chosen only when nothing else fits: of 7 decided mutations with add_persistent_state, none
# landed in band (4 stayed 5/5, 1 went 5 → 4, 2 failed the leak review), while
# extend_cross_component_dependency and introduce_global_invariant took 3 tasks into the band.
LAST_RESORT = ("add_persistent_state",)

# How the passing agents won → the mutations that defeat it (docs/ANALYSIS.md §5). Each
# rule: (name, phrases in the pass review's fix_applied / simple_strategy, code patterns
# in the passing runs' commands, operators, why). Commands are matched only on specific
# code patterns: plain words like "set" or "lock" appear in ordinary commands.
_FIX_RULES: list[tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...], str]] = [
    ("locally_plausible_strategy",
     ("greedy", "first-fit", "first fit", "heuristic", "locally", "sort by"),
     ("sorted(", ".sort("),
     ("generate_counterexample",),
     "a locally reasonable strategy passed; add an input where it is globally wrong"),
    ("local_reorder_or_serialize",
     ("reorder", "serializ", "lock", "mutex", "single thread", "sequential", "moved the", "swap"),
     ("threading.Lock(", "asyncio.Lock(", "sync.Mutex", "synchronized(", "with lock"),
     ("add_concurrent_execution", "compose_failure_modes"),
     "a local reorder or lock passed; concurrent paths or a second failure must defeat it"),
    ("process_local_guard",
     # Only phrases that name a guard: "cache", "set of", "dict of", "flag" and "in memory"
     # also describe fixes that aren't guards (a cache invalidation, "the set of jobs", a store
     # rebuilt in memory from its WAL in aws-iter1-t06).
     ("seen set", "process-local", "module-level", "global variable", "class attribute"),
     # A Python `global` statement starts its line; a bare "global " also matched C text such as
     # "the global optimum" in aws-agent-b1-t08's planner.
     ("= set()", "= {}  #", "processed = set", "seen = set", "_seen", "\nglobal ", "    global "),
     ("add_persistent_state", "inject_partial_failure"),
     "the passing fix kept a guard in one process; a restart or crash must defeat it"),
    ("self_checking_agent",
     ("fuzz", "differential", "property", "brute force", "brute-force", "random test", "own test",
      "exhaustive"),
     ("import hypothesis", "random.randint", "random.choice", "itertools.permutations", "for seed in"),
     ("introduce_global_invariant", "generate_counterexample"),
     "the agents tested their own fix and self-corrected; add a requirement their self-check can't see"),
    ("timing_dependent",
     ("retry", "sleep", "timeout", "backoff"),
     ("time.sleep(",),
     ("introduce_temporal_constraint",),
     "the fix leaned on timing; the order of operations must matter"),
    ("special_case",
     ("special-case", "special case", "hardcod", "edge case"),
     (),
     ("add_semantic_edge_case",),
     "a special case passed; the rule must hold across cases it doesn't name"),
]

# The pass review's suggestions[].technique (prompting.HARDENING_TECHNIQUES) → operator;
# a trusted review's suggestions take the slots after remove_cues, ahead of the rules.
TECHNIQUE_OPERATORS = {
    "interacting_requirements": "introduce_global_invariant",
    "counterfactual_failure": "generate_counterexample",
    "state_and_history": "add_persistent_state",
    "partial_observability": "add_realistic_distractor",
    "nonlocal_cause": "extend_cross_component_dependency",
    "adversarial_edge_cases": "add_semantic_edge_case",
    "locally_correct_globally_wrong": "generate_counterexample",
    "dependent_subproblems": "compose_failure_modes",
    "preserve_guarantees": "extend_cross_component_dependency",
    "genuine_tradeoff": "tighten_resource_bound",
}


_WRITES = ("cat >", "tee ", "sed -i", "patch ", "write_text", "open(")


def _edits_app(command: str) -> bool:
    """A solver command that writes the task's code: it writes a file, and not a /tmp helper
    (a `python3 - <<PY` benchmark writes nothing and doesn't count either)."""
    return any(w in command for w in _WRITES) and "/tmp/" not in command


def choose_mutations(pass_count: int, rollouts: list[dict], analysis: dict | None,
                     history: list[dict]) -> dict[str, Any]:
    """Which world mutations the next version applies, from the evidence (fixed rules, no model).

    Returns {"operators", "why", "rules", "target_fix"}: `rules` says, per operator, which
    rule fired on what evidence, so we can measure which rules move the pass rate. Too easy:
    up to MAX_MUTATIONS operators aimed at the strategy that passed; cues in the pass review
    mean the instruction leaks, so remove_cues comes first, on top of them (it removes a
    leak, it adds no difficulty). Otherwise no operators (the
    revision eases or repairs the task) and target_fix is the fix that failed. Operators
    already in `history` come last, so successive versions try something new; the defaults
    favor hidden dependents and global invariants over size."""
    if not too_easy(pass_count):
        failed = [(r.get("failure") or {}).get("explanation") for r in sorted(rollouts, key=lambda r: r["slot"])
                  if r["outcome"] != Outcome.VALID_PASS]
        return {"operators": [], "why": [], "rules": [], "target_fix": next((f for f in failed if f), None)}

    analysis = analysis if (analysis or {}).get("trusted") else {}
    passing = [r for r in rollouts if r["outcome"] == Outcome.VALID_PASS]
    # Only commands that write the task's code: a solver's own helper scripts (benchmarks,
    # checkers) aren't the fix (aws-iter2-t04 v3 got inject_partial_failure from `= set()` in a
    # Python benchmark of a Java task).
    commands = "\n".join(cmd for r in passing for c in (r.get("trace_metrics") or {}).get("tool_calls") or []
                         if _edits_app(cmd := c.get("input", "")))
    said = " ".join(filter(None, [analysis.get("fix_applied"), analysis.get("simple_strategy")])).lower()

    hits: list[dict[str, str]] = []  # {operator, why, rule, evidence}

    def hit(operators, why, rule, evidence):
        hits.extend({"operator": op, "why": why, "rule": rule, "evidence": evidence} for op in operators)

    if analysis.get("cues"):
        quotes = "; ".join(f'"{c.get("quote")}"' for c in analysis["cues"][:3])
        hit(("remove_cues",), "the instruction points at the fix; rewrite those sentences at the interface level",
            "cues_found", f"pass review cues: {quotes}")
    # The reviewer read the passing traces: its first suggestion outranks keyword matches on
    # them; the rest come after the rules (its second is nearly always state_and_history).
    suggested = [(op, s) for s in analysis.get("suggestions") or []
                 if (op := TECHNIQUE_OPERATORS.get(s.get("technique")))]

    def suggest(pairs):
        for op, s in pairs:
            hit((op,), s.get("change") or f"suggested: {s['technique']}", "pass_review_suggestion",
                f"technique {s['technique']}")

    suggest(suggested[:1])
    touched = [len(f) for r in passing if (f := (r.get("trace_metrics") or {}).get("files_modified")) is not None]
    if touched and median(touched) <= 1:
        hit(("extend_cross_component_dependency",), "the passing runs edited one file; the fix must span modules",
            "one_file_patch", f"median files edited {median(touched):g}")
    for rule, phrases, patterns, operators, why in _FIX_RULES:  # most specific first
        phrase = next((p for p in phrases if p in said), None)
        pattern = next((p for p in patterns if p in commands), None)
        if phrase or pattern:
            hit(operators, why, rule, f'pass review said "{phrase}"' if phrase else f"command contained `{pattern}`")
    steps = [s for r in passing if (s := (r.get("trace_metrics") or {}).get("llm_calls"))]
    if steps and median(steps) <= 8 and not analysis.get("cues"):
        hit(("add_realistic_distractor",), "the agents went straight to the right place; add a plausible wrong lead",
            "straight_to_the_fix", f"median {median(steps):g} agent steps")
    suggest(suggested[1:])
    hit(("introduce_global_invariant",), "make independently valid actions interact", "default", "no specific rule")
    hit(("generate_counterexample",), "add an input where the plausible fix is wrong", "default", "no specific rule")

    hits.sort(key=lambda h: h["operator"] in LAST_RESORT)  # stable: the rest keep their order
    used = {op for m in history for op in m.get("operators") or []}
    chosen: list[dict[str, str]] = [h for h in hits[:1] if h["operator"] == "remove_cues"]  # a leak first, always
    harder = 0
    for fresh in (True, False):
        for h in hits:
            if (harder < MAX_MUTATIONS and h["operator"] != "remove_cues" and (h["operator"] not in used) == fresh
                    and h["operator"] not in {c["operator"] for c in chosen}):
                chosen.append(h)
                harder += 1
    return {"operators": [h["operator"] for h in chosen], "why": [h["why"] for h in chosen],
            "rules": [{"operator": h["operator"], "rule": h["rule"], "evidence": h["evidence"]} for h in chosen],
            "target_fix": analysis.get("fix_applied") or analysis.get("simple_strategy")}
