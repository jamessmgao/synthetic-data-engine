"""
Authoring prompts, rendered deterministically from a spec.

A spec is what the Job Service samples for a candidate (PRD §6.2, §6.9):

    {
      "taxonomy": {"domain": ..., "task_type": ..., "core_capability": ...,
                   "failure_mode": ..., "environment": ..., "skill_interaction": ...},
      "difficulty": {"level": "medium-hard", "relevant_files": "15-25",
                     "constraints": 3, "subgoals": 2, "failure_scenarios": 1,
                     "notes": ["Root-cause investigation across multiple modules"]},
      "design": ["optional extra design requirements, one per line"]
    }

Repair and revision prompts continue the same conversation: the original
prompt, the previous draft as the assistant's turn, then the request.
"""

import json

from shared.taskformat.schema import TaskDraft

TAXONOMY_LABELS = [
    ("domain", "Domain"),
    ("task_type", "Task type"),
    ("core_capability", "Core capability"),
    ("failure_mode", "Primary failure mode"),
    ("environment", "Environment"),
    ("skill_interaction", "Skill interaction"),
]

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

Generate the task and its artifacts.

Do not claim that the task meets the target difficulty. The pipeline will
validate the environment and run five independent agent evaluations to
measure it."""


def render_initial_prompt(spec: dict) -> str:
    tax = spec.get("taxonomy", {})
    diff = spec.get("difficulty", {})
    lines = [
        "You are an expert author of realistic, executable Terminal-Bench-style software engineering tasks.",
        "",
        "Create a NEW task from scratch. This is the first generation of the task; no previous task or "
        "agent evaluation results exist.",
        "",
        "TASK TAXONOMY",
        "",
    ]
    lines += [f"{label}: {tax[key]}" for key, label in TAXONOMY_LABELS if tax.get(key)]
    lines += ["", "TARGET DIFFICULTY", "", f"Target level: {diff.get('level', 'medium-hard')}", "",
              "The task should require:"]
    if diff.get("relevant_files"):
        lines.append(f"- Approximately {diff['relevant_files']} relevant repository files")
    if diff.get("constraints"):
        lines.append(f"- {diff['constraints']} substantive correctness constraints")
    if diff.get("subgoals"):
        lines.append(f"- {diff['subgoals']} dependent engineering subgoals")
    if diff.get("failure_scenarios"):
        lines.append(f"- {diff['failure_scenarios']} reproducible failure scenario(s)")
    lines += [f"- {note}" for note in diff.get("notes", [])]
    lines += ["", "These are authoring targets, not guarantees of difficulty. Do not add irrelevant files or "
              "artificial complexity merely to satisfy numerical targets."]
    if spec.get("design"):
        lines += ["", "TASK DESIGN REQUIREMENTS", ""] + [f"- {d}" for d in spec["design"]]
    lines += ["", FIXED_SECTIONS]
    return "\n".join(lines) + "\n"


REPAIR_REQUEST = """The pipeline built and ran your task, and it failed a gate:

{feedback}

Return the complete corrected task as one JSON object matching the schema.
Change only what is needed to fix these problems. Keep every correctness
constraint and every test function: you may correct a test that is itself
wrong, but do not delete or weaken a test to make the reference solution pass."""

REVISION_REQUEST = """Five independent agent runs were made on your task. {result}

Revise the task: {operator_instruction}

Evidence from the runs:
{evidence}

Return a complete new version of the task as one JSON object matching the
schema. Keep it realistic and fair: every requirement the tests check must be
stated in the instruction, and the task must stay solvable."""

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


def continuation(prompt: str, previous: TaskDraft, request: str) -> list[dict]:
    """The user prompt, the previous draft as the model's own turn, then the request."""
    return [
        {"role": "user", "content": prompt},
        {"role": "assistant", "content": json.dumps(previous.model_dump(), indent=1)},
        {"role": "user", "content": request},
    ]
