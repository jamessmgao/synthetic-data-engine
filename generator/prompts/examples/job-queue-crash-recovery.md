You are an expert author of realistic, executable
Terminal-Bench-style software engineering tasks.

Create a NEW task from scratch. This is the first
generation of the task; no previous task or agent
evaluation results exist.

TASK TAXONOMY

Domain: Distributed systems
Task type: Debugging
Core capability: Reliable background-job processing
Primary failure mode: Worker crash and recovery
Language: Python

TARGET DIFFICULTY

Target level: Medium-hard

The task should require:
- Approximately 15–25 relevant repository files
- 3 substantive correctness constraints
- 2 dependent engineering subgoals
- 1 reproducible failure scenario
- Root-cause investigation across multiple modules

These are authoring targets, not guarantees of
difficulty. Do not add irrelevant files or artificial
complexity merely to satisfy numerical targets.

TASK DESIGN REQUIREMENTS

Create a realistic Python background-job processing
service with an existing implementation that contains
a meaningful reliability bug.

The intended engineering problem should involve a
worker that may crash during job processing.

Design the task so the agent must:
1. Investigate the worker's processing and
   acknowledgment behavior.
2. Implement a correction that preserves the
   required reliability guarantees.

Include three meaningful correctness constraints,
such as:
- Jobs must not be silently lost.
- Recovered jobs must not duplicate committed effects.
- Transient failures must remain recoverable.

Ensure these requirements are consistent with the
provided system's delivery semantics and storage
capabilities. If exactly-once effects require
idempotency keys or transactional state, provide
the necessary mechanisms.

INSTRUCTION QUALITY

- Describe the objective, constraints, and expected
  behavior clearly.
- Make all necessary information discoverable.
- Do not reveal the exact implementation.
- Do not prescribe one specific patch.
- Do not introduce unrelated work to increase difficulty.
- Do not copy a human-authored example verbatim.

ENVIRONMENT REQUIREMENTS

- Create a self-contained, reproducible environment.
- Provide a Dockerfile with appropriate dependencies.
- Include a runnable but intentionally flawed service.
- Include realistic source code and configuration.
- The agent must be able to inspect and modify the code.

TEST REQUIREMENTS

- Create tests for all stated correctness constraints.
- Include happy-path and failure-recovery tests.
- Use deterministic fault injection where possible.
- Tests must evaluate observable behavior.
- The reference solution must pass all tests.
- The unmodified starting implementation must fail
  at least one substantive test.

OUTPUT ARTIFACTS

1. Structured task specification
2. instruction.md
3. Dockerfile
4. Starting source-code repository
5. Automated verifier tests
6. Reference solution or solution patch
7. Requirement-to-test coverage mapping

Generate the task and its artifacts.

Do not claim that the task meets the target difficulty.
The pipeline will validate the environment and run
five independent agent evaluations to measure it.
