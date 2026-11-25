"""The hardening techniques, by name: the Trace Parser's pass review picks from them and the
revision prompts list them. Plain data."""

# Ways to make a task harder in depth of reasoning, not in size. The Trace
# Parser's pass review picks from these by name; the revision prompt lists them.
HARDENING_TECHNIQUES = {
    "interacting_requirements": "Make correctness guarantees depend on one another, so the straightforward fix "
                                "for one violates another and the trade-off has to be understood.",
    "counterfactual_failure": "Make a reasonable-looking implementation fail when events happen differently "
                              "(another order, a retry, a crash, a concurrent caller), not only on the normal path.",
    "state_and_history": "Make correctness depend on state over time: transitions, delayed effects, retries, "
                         "recovery, changing conditions. What is right in one state is wrong in another.",
    "partial_observability": "Spread the evidence for the root cause across logs, components or execution paths, "
                             "so it has to be investigated and reconciled; everything needed stays available.",
    "nonlocal_cause": "Make the symptom appear in a different component from the root cause, so dependencies and "
                      "data flow have to be traced instead of patching where the symptom shows.",
    "adversarial_edge_cases": "Add realistic failure sequences, unusual inputs, concurrent operations or boundary "
                              "conditions that defeat the naive solution by exposing a real design weakness.",
    "locally_correct_globally_wrong": "Make a greedy decision, isolated fix or simple heuristic look right but "
                                      "violate a stated overall requirement.",
    "dependent_subproblems": "Require several connected decisions where earlier choices constrain later ones, so "
                             "a partial solution is not enough.",
    "preserve_guarantees": "Require the change without regressions: other behavior, including what hidden "
                           "dependents rely on, must keep working (tested by a regression suite).",
    "genuine_tradeoff": "Require balancing correctness with progress, concurrency, efficiency or reliability, "
                        "without a trivial solution that disables one of them.",
}
