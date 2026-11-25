"""
Authoring prompts, rendered deterministically from a spec.

A spec comes from POST /v1/tasks (api/submit.py): sampled from the taxonomy,
given explicitly, or built from a requester's free-text idea (`seed`) (PRD §6.2, §6.9):

    {
      "seed": "optional free-text task idea; the task is built around it",
      "taxonomy": {"domain": ..., "task_type": ..., "core_capability": ...,
                   "failure_mode": ..., "environment": ..., "skill_interaction": ...},
      "difficulty": {"form": "behavior_preserving", "dial": 2,
                     "notes": ["optional extra difficulty notes"]},
      "design": ["optional extra design requirements, one per line"]
    }

The user message carries only what varies per task: the idea or taxonomy, a
concrete task FORM, and a difficulty DIAL inside that form. Every rule lives
in the system prompt (prompts/system.md). The pilots showed the generic
difficulty counts (files, constraints, subgoals) do not move glm-5.3-flash's
pass rate; the task's form does (docs/DIFFICULTY.md). Older difficulty
keys (level, relevant_files, constraints, subgoals, failure_scenarios) are
ignored. A spec without a form gets DEFAULT_FORM, including seed-only specs.

`difficulty.fixed_sections: true` re-appends the original example prompt's
fixed sections, for the A/B that checks whether dropping them costs validity
(experiments/prompt_ab.py).

Repair and revision prompts continue the same conversation: the original
prompt, the previous draft as the assistant's turn, then the request.
"""

import json

from shared.contracts.hardening import HARDENING_TECHNIQUES
from shared.taskformat.schema import TaskDraft

TAXONOMY_LABELS = [
    ("domain", "Domain"),
    ("task_type", "Task type"),
    ("core_capability", "Core capability"),
    ("failure_mode", "Primary failure mode"),
    ("environment", "Environment"),
    ("skill_interaction", "Skill interaction"),
]

# Task forms: what kind of task to build, chosen because of how glm-5.3-flash
# fails (docs/DIFFICULTY.md §6). Each has a dial from 1 (easier) to 3.
FORMS = {
    # The only form that produced a genuine model failure in the pilots (f1:
    # the fix broke a module that silently relied on the old behavior).
    "behavior_preserving": {
        "title": "Coupled behavior-preserving change",
        "lines": [
            "The repository is a realistic system in which one shared component (a utility module, a library "
            "function, a data model) has a defect or needs a stated behavior change. The instruction states the "
            "symptoms or the change in terms of the system's interface: commands, inputs, outputs, files.",
            "Other parts of the system depend on the component's current behavior, including at least one "
            "dependency that is invisible at the call site (a side effect, an ordering assumption, shared "
            "mutable state, a cached or derived value). The obvious local fix breaks it.",
            "The instruction states, at the interface level, every behavior that must keep working, including "
            "the behaviors the hidden dependents provide, without saying which module depends on what or why.",
            "The tests include a regression suite derived from the original system's behavior across the "
            "dependent features (stored only in the tests), plus tests for the stated change. A plausible fix "
            "that passes the agent's own obvious checks must fail at least one of them.",
        ],
        "dial_name": "hidden dependents of the changed behavior",
        "dial": {
            1: "One other module relies on the old behavior, and the dependency is visible on careful reading.",
            2: "Two modules rely on the old behavior; at least one dependency is invisible at the call site.",
            3: "Three or more modules rely on the old behavior, one of them indirectly (through a derived value "
               "or a later pipeline stage), and two dependencies are invisible at the call site.",
        },
    },
    # A long list of precisely stated, interacting rules: the agent's own
    # tests tend to pass while one subtle rule is missed.
    "many_rules": {
        "title": "Implementation against many precise rules",
        "lines": [
            "The agent implements one component (a stub that raises 'not implemented') that plugs into a "
            "realistic existing system, from a specification of numbered rules stated in the instruction.",
            "Each rule is short and precise; several rules interact (precedence, ordering, normalization, "
            "tie-breaking, boundary values, error cases). Every rule is stated exactly once, with a small "
            "example where it helps, and nothing the tests check is missing from the rules.",
            "The tests generate many inputs at verify time from a fixed seed, designed to exercise every rule "
            "and rule interaction, and compare the output with a reference model that lives only in the tests. "
            "Group the tests by rule so a failure names the rule that was missed.",
        ],
        "dial_name": "number and coupling of rules",
        "dial": {
            1: "About 8 numbered rules, two of which interact.",
            2: "12 to 14 numbered rules, several of which interact.",
            3: "16 to 20 numbered rules with chains of interaction (one rule's output feeds another's "
               "precedence or tie-break).",
        },
    },
}
DEFAULT_FORM = "behavior_preserving"
DEFAULT_DIAL = 2

ROLE_LINES = [
    "You are an expert author of realistic, executable Terminal-Bench-style software engineering tasks.",
    "",
    "Create a NEW task from scratch. This is the first generation of the task; no previous task or "
    "agent evaluation results exist.",
]

CLOSING = ("Do not claim that the task meets the target difficulty. The pipeline will validate the "
           "environment and run five independent agent evaluations to measure it.")

# The original example prompt's fixed sections. They restate system.md and are
# kept only for the A/B (difficulty.fixed_sections).
FIXED_SECTIONS = """INSTRUCTION QUALITY

- Describe the objective, constraints, and expected behavior clearly.
- Make all necessary information discoverable.
- Do not reveal the root cause, the files to change, or the exact implementation.
- Do not prescribe one specific patch.
- Do not introduce unrelated work to increase difficulty.
- Do not copy a human-authored example verbatim.

ENVIRONMENT REQUIREMENTS

- Create a self-contained, reproducible environment.
- Provide a Dockerfile with appropriate dependencies.
- Include a runnable but intentionally flawed system.
- Include realistic source code and configuration.
- The agent must be able to inspect and modify the code.

TEST REQUIREMENTS

- Create tests for all stated correctness constraints.
- Include happy-path and failure tests.
- Use deterministic fault injection where possible.
- Tests must evaluate observable behavior.
- The reference solution must pass all tests.
- The unmodified starting implementation must fail at least one substantive test.

OUTPUT ARTIFACTS

1. Structured task specification
2. instruction.md
3. Dockerfile
4. Starting source-code repository
5. Automated verifier tests
6. Reference solution
7. Requirement-to-test coverage mapping

Generate the task and its artifacts."""


def form_and_dial(spec: dict) -> tuple[str, int]:
    diff = spec.get("difficulty") or {}
    form = diff.get("form") or DEFAULT_FORM
    if form not in FORMS:
        raise ValueError(f"unknown task form {form!r}; known: {sorted(FORMS)}")
    dial = int(diff.get("dial") or DEFAULT_DIAL)
    return form, min(max(dial, 1), 3)


def render_initial_prompt(spec: dict) -> str:
    tax = spec.get("taxonomy") or {}
    diff = spec.get("difficulty") or {}
    form_name, dial = form_and_dial(spec)
    form = FORMS[form_name]

    lines = list(ROLE_LINES)
    if spec.get("seed"):
        # A requester's free-text idea (POST /v1/tasks {"prompt": ...}): the task is
        # built around it, still in the form below.
        lines += ["", "TASK IDEA (from the requester)", "", spec["seed"].strip(), "",
                  "Build the task around this idea, in the task form below. Where the idea leaves the "
                  "domain, environment or failure mode open, choose ones that fit it realistically."]
    if any(tax.get(key) for key, _ in TAXONOMY_LABELS):
        lines += ["", "TASK TAXONOMY", ""]
        lines += [f"{label}: {tax[key]}" for key, label in TAXONOMY_LABELS if tax.get(key)]
    lines += ["", f"TASK FORM: {form['title']}", ""] + [f"- {line}" for line in form["lines"]]
    lines += ["", f"DIFFICULTY DIAL: {form['dial_name']}, level {dial} of 3", "", f"- {form['dial'][dial]}"]
    lines += [f"- {note}" for note in diff.get("notes", [])]
    if spec.get("design"):
        lines += ["", "TASK DESIGN REQUIREMENTS", ""] + [f"- {d}" for d in spec["design"]]
    if diff.get("fixed_sections"):
        lines += ["", FIXED_SECTIONS]
    lines += ["", CLOSING]
    return "\n".join(lines) + "\n"


REPAIR_REQUEST = """The pipeline built and ran your task, and it failed a gate:

{feedback}

Return the complete corrected task as one JSON object matching the schema.
Change only what is needed to fix these problems. Keep every correctness
constraint and every test function: you may correct a test that is itself
wrong, but do not delete or weaken a test to make the reference solution pass."""

HARDER_GUIDE = """How to make it harder:

First, find why the current task is easy: the simple strategy that solved it and
the cues that led the agents straight to it (the analysis above says what the
agents actually did). Then make one or two careful semantic changes that make
exactly that strategy insufficient, using one or more of these techniques:

{techniques}

Make the reasoning deeper, not the task bigger. The ideal harder task is one
where a competent engineer writes a plausible solution that works in ordinary
cases, but has to discover a deeper interaction, state dependency or
counterexample to get it fully right. Do NOT make it harder through vague
instructions, hidden or unstated requirements, more code, larger inputs alone,
arbitrary restrictions, or unreliable tests. State every new requirement in the
instruction, at the level of the system's interface, without describing the fix.
Keep it coherent, realistic, solvable and objectively verifiable."""

EASIER_GUIDE = """How to make it easier: keep the core engineering problem, but remove what made it
unreachable. Reduce the number of interacting requirements, make the relevant
evidence easier to find, or narrow where the problem can be, without naming the
defect or describing the fix."""

REVISION_REQUEST = """Five independent agent runs were made on your task. {result} {verdict}

{analysis}Evidence from the runs:
{evidence}

{guide}

Suggested focus: {operator_instruction}

Return a complete new version of the task as one JSON object matching the
schema. Keep it realistic and fair: every requirement the tests check must be
stated in the instruction, and the task must stay solvable."""

TARGET_BAND = "The target is a task that 1 to 3 of 5 runs pass."

# PRD §6.6 revision operators: what GLM-5.1 is asked to change.
OPERATORS = {
    "increase_constraint_interaction": "add a correctness constraint that the obvious fix breaks, so the "
                                       "constraints interact.",
    "increase_diagnosis_depth": "remove redundant hints from the instruction and code; show symptoms instead "
                                "of the cause, so the root cause has to be found.",
    "increase_cross_component_dependency": "make the correct fix span two modules that depend on each other.",
    "reduce_unrelated_constraints": "drop the correctness constraint (and its tests) that no run got close to, "
                                    "keeping the core problem intact.",
    "localize_diagnosis": "point the instruction at the subsystem where the problem lives, without naming the "
                          "defect itself.",
    # Chosen by the Trace Parser when its review blames a failure on the task, not the agent.
    "repair_ambiguity": "fix the defect the reviewer found in the task itself: state every requirement the "
                        "tests check in the instruction, or fix the broken test or environment. Keep the "
                        "difficulty unchanged.",
}


EASIER_OPERATORS = {"localize_diagnosis", "reduce_unrelated_constraints"}


def revision_direction(job_input: dict) -> str:
    """harder | easier | repair, given or inferred from the operator."""
    if job_input.get("direction") in ("harder", "easier", "repair"):
        return job_input["direction"]
    op = job_input.get("operator")
    if op == "repair_ambiguity":
        return "repair"
    return "easier" if op in EASIER_OPERATORS else "harder"


def format_pass_analysis(analysis: dict | None) -> str:
    """The Trace Parser's review of the passing runs, as a prompt section."""
    if not analysis or analysis.get("trusted") is False \
            or not (analysis.get("why_easy") or analysis.get("simple_strategy")):
        return ""  # missing, failed, or cites no real evidence: don't steer the revision with it
    lines = ["Why the runs passed (a review of the passing agents' traces):"]
    for key, label in (("why_easy", "Why it was easy"), ("simple_strategy", "The strategy that worked"),
                       ("fix_applied", "The fix the agents applied")):
        if analysis.get(key):
            lines.append(f"- {label}: {analysis[key]}")
    cues = [c for c in analysis.get("cues") or [] if c.get("quote")]
    if cues:
        lines.append("- Cues that led the agents straight to it:")
        lines += [f"  - \"{c['quote']}\" ({c.get('where', 'task')})" for c in cues]
    suggestions = analysis.get("suggestions") or []
    if suggestions:
        lines.append("- Suggested changes (each aimed at the strategy above):")
        for i, s in enumerate(suggestions, 1):
            lines.append(f"  {i}. [{s.get('technique')}] {s.get('change')} (defeats: {s.get('defeats')})")
    return "\n".join(lines) + "\n\n"


def render_revision_request(job_input: dict) -> str:
    direction = revision_direction(job_input)
    if direction == "harder":
        verdict = f"That is too easy. {TARGET_BAND}"
        techniques = "\n".join(f"- {name}: {text}" for name, text in HARDENING_TECHNIQUES.items())
        guide = HARDER_GUIDE.format(techniques=techniques)
    elif direction == "easier":
        verdict, guide = f"That is too hard. {TARGET_BAND}", EASIER_GUIDE
    else:
        verdict, guide = "A review found that the task itself is defective.", (
            "Fix the defect without changing the difficulty.")
    operator = job_input.get("operator")
    return REVISION_REQUEST.format(
        result=job_input.get("result", "").strip(), verdict=verdict,
        analysis=format_pass_analysis(job_input.get("analysis")) if direction == "harder" else "",
        evidence=job_input.get("evidence") or "(none recorded)", guide=guide,
        operator_instruction=OPERATORS.get(operator, "use the analysis above") if operator else "use the analysis above",
    )


def continuation(prompt: str, previous: TaskDraft, request: str) -> list[dict]:
    """The user prompt, the previous draft as the model's own turn, then the request."""
    return [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": json.dumps(previous.model_dump(), indent=1)},
        {"role": "user", "content": request},
    ]
