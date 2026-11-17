# Task Generation Rules

How the generator controls task difficulty and diversity.

## Difficulty controls

Configuration is split into two kinds of levers: **quantitative** levers, which code can measure, and **semantic** levers, which the generator controls.

### Quantitative levers (measurable through code)

| Lever | Example |
|---|---|
| Input size | 10 files vs. 100 files |
| Component count | 1 module vs. 4 interacting modules |
| Constraint count | Correctness only vs. correctness + ordering + idempotency |
| Required scenarios | Normal execution vs. normal execution + crash recovery |
| Dependency depth | 1 sequential subgoal vs. 3 dependent subgoals |

These can be defined, generated, and validated fairly deterministically. For example, code can confirm that a repository has 30 files or that a test fixture contains 10,000 jobs.

Consequential decision points are harder to count. GLM-5.1 can be asked to generate three dependent engineering decisions, but counting three decisions in the task specification doesn't prove the agent actually has to make them.

**Rule:** treat decision-point count as a generation target, then examine agent trajectories to check whether those decisions were actually necessary.

### Semantic levers (controlled by the generator)

These properties need more interpretation.

| Lever | Easier | Harder |
|---|---|---|
| Diagnosis difficulty | Bug identified in instructions | Agent must discover root cause |
| Constraint interaction | Requirements independent | Fixing one can break another |
| Information discovery | Relevant logs provided directly | Agent must determine which evidence matters |
| Failure complexity | Happy-path behavior | Concurrency, partial failures, recovery |
| Planning complexity | One obvious implementation step | Multiple dependent decisions |

**Constraint interaction** and **diagnosis difficulty** matter most. A task with 20 independent requirements can be easier than a task with two subtle requirements that conflict under concurrency.

**Rule:** keep both numerical controls and qualitative instructions in each generation spec. Don't reduce difficulty to counts alone.

## Taxonomy

Each task is tagged along these dimensions. Spread the batch across them so it stays diverse.

| Dimension | Example values |
|---|---|
| Domain | Distributed systems, databases, networking, security, ML infrastructure |
| Task type | Debugging, implementation, migration, optimization, investigation |
| Core capability | Concurrency, consistency, resource management, performance |
| Failure mode | Race condition, crash, timeout, resource exhaustion |
| Environment | Python service, Java application, Linux container, database |
| Required skill interaction | Single skill, two dependent skills, multiple interacting constraints |
