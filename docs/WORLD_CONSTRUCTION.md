# Generating Difficult Software Engineering Tasks Using World Construction and Iterative Evolution

## 0. As built (agent mode)

Sections 1-13 below are the method and its rationale. This section is what the pipeline
actually does today (`SDE_AUTHOR_MODE=agent`); where they differ, this section is current.

### The world job

A `world` job (`generator/author.py:run_world_job`, contract in docs/WORKER_INTERFACE.md) is
one authoring trial, terminus-2 + GLM-5.1 in the authoring image (`authoring/Dockerfile`),
with the instruction from `generator/prompts/world.md` (initial, repair) or
`generator/prompts/world_mutation.md` (mutation), plus the family brief
(`generator/families.py:brief_for`). It writes the world to `/logs/artifacts/world/`; the
pipeline stores it content-addressed at `<work>/worlds/<sha>/` and sets world.json's `spec`
and `lineage`.

**Budget: 30 minutes** (`SDE_WORLD_TIMEOUT_S`, default 1800): the system running by about 10,
the counterexample replaying by about 20, then the self-check. (P1's first real world took
5.4 min and $0.51.) **Scope: about 300-800 lines of code in 2-4 modules.** Difficulty comes
from components that interact and from hidden dependents, never from size.

### The files

| file | what it is |
|---|---|
| `environment/` | the system, with its `Dockerfile` (`FROM` one of `config/base_images.json`; the system at `/app` by default) |
| `repro/repro.sh` | the deterministic reproduction of the deep schedule: exit 1 = an invariant is violated, 0 = it holds, the same every run |
| `repro/symptom_check.sh` | the visible symptom only, the first reproduction an agent would write (one crash, one retry, the reported input): 1 on the world, 0 once the symptom is gone |
| `world_fix/fix.sh` | the real fix, editing the world in place |
| `world_fix/wrong_fix.sh` | the plausible wrong fix, implemented |
| `repro/dependent_check.sh` | needed when `fix.sh` changes one file: the dependent module's behavior, 0 on the world and after the fix, 1 after the wrong fix |
| `world.json` | `{spec, name, invariants, counterexample: {sequence, reproduce, observed}, plausible_wrong_fix, lineage}`; the pipeline fills `spec` and `lineage` |

Every script runs inside the world's image, copied to `/world/repro/` and `/world/world_fix/`,
from the image's `WORKDIR`, so it reaches the system by absolute paths (`/app/...`).

### What makes a world hard (the rules in world.md)

- **Invariants are outcomes**, never mechanisms ("each request ID debits its quantity at most
  once", not "later deliveries must reuse the stored result"): the task instruction is built
  from them, and a mechanism there is the fix.
- **A local patch must not be enough**: the correct fix spans 2+ modules, or a module
  elsewhere depends on the current behavior (a derived value, a cache, a report, a downstream
  consumer), so the obvious one-file patch breaks it. Each family brief names its typical
  dependent.
- **The plausible wrong fix** makes the simple visible symptom go away and fails only on a
  deeper schedule (another crash point, a restart, a concurrent worker) or on the dependent.
- **No documented fault hooks or stage names**: no env var, flag or command that crashes the
  system at a named stage (P1's `STOP_AFTER=inventory` told the agent where the bug was).
  Faults are injected from outside, as the tests will: kill the process at seeded points from
  a test-side harness, or drive `sde_fault` from the repro and tests, never documented, with
  neutral point names. The system only has to survive being killed at any moment.
- **Not announced, not padded, no unstated values** (comments, logs, names or README text
  that point at the defect; filler; values a task needs but the world doesn't expose).

### The scaffolds (`authoring/scaffold/`, `/opt/sde/scaffold` in the image)

Small, tested, stdlib-only helpers the world vendors into `environment/` instead of writing
its own (`authoring/scaffold/README.md`):

- `sde_sched.py`: deterministic interleavings. Actors are generators that `yield` at each point
  another may run; `Scheduler(seed=…)` or `Scheduler(replay=schedule)` picks the order;
  `explore(build, check, seeds)` returns the first schedule that breaks an invariant;
  `save_schedule` / `load_schedule` for `repro/`.
- `sde_fault.py`: a fault plan (dict, file or `SDE_FAULTS`): `point(name)` crashes on the
  planned hit, `deliver(kind, msg)` drops or duplicates the Nth message, `write(f, data, at=)`
  tears a write; `fired` logs what happened.
- `sde_repro.py`: `repro_main(run, INVARIANTS)` replays `repro/sequence.json` and exits
  1 violated / 0 holds / 2 harness broken.
- For C and Go, the README gives the same patterns inline (a crash point read from the
  environment, goroutines released one step at a time); its C snippet is compiled in the tests.

### The self-check: `sde-world check` (`authoring/sde_world.py`)

Runs the `world_check` gate (`shared/taskformat/world_check.py:run_world_check_job`) on the world and
saves the result to `/logs/artifacts/world_check.json`, which the pipeline reads
(`generator/self_check.py:ensure_world_checked`). The base checks (repro fails twice, passes
twice after the fix) must pass. The hardness checks (symptom visible and removed by the fix;
the fix spans 2+ files or breaks a dependent; the wrong fix passes the symptom check and fails
the repro) are enforced, recorded or skipped per `SDE_WORLD_HARDNESS` (warn by default), and
each failed one prints a `WARNING:` with what to change, for example:

```
WARNING: the plausible wrong fix does not pass symptom_check.sh, so it is not tempting: make it fix the visible symptom
WARNING: fix.sh changes 1 file(s) ['/app/store.py'] and there is no working dependent_check.sh: make the fix span 2+ modules, or add a module that depends on the current behavior and a repro/dependent_check.sh the local patch breaks
```

The author is told to fix every WARNING before finishing if time allows. Details of every
check: docs/VALIDATION_GATES.md.

### Mutations (stage 7 as built)

A too-easy version (4-5 of 5 runs passed) gets a `world` job with origin `mutation`
(`evaluation/trace_parser_service.py:next_job`). The operators come from fixed rules over the
evidence (`evaluation/calibration.py:choose_mutations`, each with the rule and evidence that
chose it): `remove_cues` first if the pass review found cues, then rules from the passing
traces and the review's suggestions. `choose_mutations` does **not** use the world check's
hardness result (that was tried and parked: docs/TODO.md, branch `park/hardness-tiebreak`).
`world_mutation.md` has a hardness section (`generator/author_task.py:hardness_section`) that
would list what the parent world missed, but nothing fills the mutation input's `hardness`
today, so it renders "(not recorded for the parent world)". When the passing strategy was a
local fix, the prompt prefers extend_cross_component_dependency, introduce_global_invariant and
generate_counterexample, and puts size-adding changes last, never alone. The mutated world
ships the same files and passes the same checks.

## 1. Core Approach

Adapt Prime Intellect's General Agent methodology to generate difficult, verifiable software engineering tasks.

The generation model should operate as an autonomous software engineer. It creates a functioning software environment, identifies ways the system can fail, constructs an engineering task around those failures, and iteratively increases difficulty based on solver performance.

The target domains are:

1. Distributed systems and concurrency.
2. Algorithmic and constraint-solving problems.
3. Performance and resource optimization.
4. Database consistency and recovery.
5. Compilers, parsers, and semantic transformations.
6. Cross-component debugging and stateful workflows.

**Central principle:** Difficulty should come from an actual technical obstacle that defeats a plausible implementation, not from arbitrary complication.

The generation pipeline is:

```text
1. Select an engineering domain
             |
             v
2. Construct an executable software world
             |
             v
3. Define its invariants and correctness requirements
             |
             v
4. Identify or construct a realistic failure mode
             |
             v
5. Create an engineering task around the failure
             |
             v
6. Produce a reference solution
             |
             v
7. Independently verify correctness
             |
             v
8. Challenge a strong coding agent
             |
             v
9. Measure solver success and analyze failures
             |
             v
10. Mutate the world/task to increase difficulty
             |
             v
11. Revalidate and repeat
```

The original approach used five difficulty tiers and 20 GPT-5-mini attempts per difficulty-calibration gate.

We can preserve that structure, but the engineering domains and mutation strategies should change.

---

# 2. Stage One: Build the Software World First

## 2.1 What is a world?

A world is a self-contained software system in which a coding agent can encounter a meaningful engineering problem.

For our purposes, a world should contain:

| Component | Purpose |
|---|---|
| Source code | The actual software implementation |
| Project structure | Multiple files, modules, and dependencies where appropriate |
| Interfaces | APIs, function contracts, protocols, or command-line operations |
| State | Data structures, persisted records, caches, or runtime state |
| Invariants | Properties the system must always preserve |
| Tests | Existing behavioral and regression tests |
| Execution harness | A reproducible way to build, run, and exercise the software |
| Fault injection | Controlled failure conditions where relevant |
| Resource constraints | Time, memory, concurrency, or throughput requirements |
| Correctness oracle | An independent way to determine whether behavior is valid |

A world can be a synthetic repository created from scratch or a carefully isolated, adapted real-world codebase.

For example, a distributed-systems world might contain:

```text
distributed-kv-store/
├── src/
│   ├── server.py
│   ├── replication.py
│   ├── consensus.py
│   ├── storage.py
│   └── recovery.py
├── tests/
│   ├── test_basic_operations.py
│   ├── test_replication.py
│   └── test_recovery.py
├── harness/
│   ├── scheduler.py
│   ├── network_faults.py
│   └── crash_injection.py
└── README.md
```

The software must actually execute. The world is not just a written description of a repository.

## 2.2 Why construct the world before the task?

### Reason A: Difficulty becomes causal

Without a world, the model might generate a complicated-sounding instruction:

"Implement a robust distributed consensus system that handles failures."

This is broad, underspecified, and difficult to grade reliably.

With a world, the generator can create a specific implementation and execution sequence in which a protocol violates a known property.

The task becomes:

"Fix the replication protocol so an acknowledged write cannot disappear after the specified failover sequence."

Now there is a concrete failure, a testable requirement, and a software artifact to modify.

### Reason B: The generator can discover failures experimentally

The generation agent can execute code, explore schedules, introduce faults, and inspect results.

For example:

```text
Generate implementation
        |
        v
Execute with concurrent clients
        |
        v
Inject leader crash
        |
        v
Observe lost acknowledged write
        |
        v
Minimize failing execution
        |
        v
Construct debugging task
```

The generated task is grounded in observed behavior instead of the model's speculation.

### Reason C: The world supplies ground truth

The environment allows correctness to be checked using executable tests, reference implementations, invariants, or mathematical specifications.

This is essential for automated task generation at scale.

### Reason D: The same world can produce many tasks

One implementation can support multiple problems:

- Fix a race condition.
- Introduce crash recovery.
- Preserve an ordering invariant.
- Reduce memory consumption.
- Support a new transactional guarantee.
- Diagnose a cross-component bug.

This is more efficient than generating an entirely unrelated codebase for every task.

### Reason E: The generator can evolve the system itself

The world is mutable.

The generator can introduce new modules, concurrency paths, transaction boundaries, workloads, and state transitions.

This means difficulty can increase by changing the engineering problem, not merely its wording.

**Important restriction:** The world must remain realistic, executable, and internally consistent. Increasing complexity by generating irrelevant files, arbitrary APIs, or meaningless constraints should not qualify as meaningful task evolution.

---

# 3. Stage Two: Define Correctness Before Generating the Problem

Before constructing the task, the generator should identify the guarantees that a correct implementation must preserve.

This is particularly important for software engineering because many bugs only appear when an implementation violates a property that is not obvious from its immediate output.

Examples:

| Domain | Example correctness invariant |
|---|---|
| Distributed systems | Every acknowledged write must satisfy the specified durability guarantee |
| Algorithms | The output must be globally optimal, not merely locally valid |
| Performance | Output must remain correct while satisfying a measured resource bound |
| Databases | Inventory must never become negative under concurrent transactions |
| Compilers | The transformed program must preserve observable behavior |
| Cross-component debugging | Recovery must preserve consistency across retries, caches, and persistent state |

A task should have an explicit correctness contract.

For instance:

```text
System: Inventory reservation service

Invariant:
For each SKU, confirmed reservations must never
exceed the available stock.

Additional requirements:
- Duplicate requests must not reserve stock twice.
- Concurrent requests must preserve the invariant.
- Retried requests must return consistent outcomes.
- Completed reservations must survive specified crashes.
```

The model can then search for implementations or execution sequences that violate these requirements.

**We should prefer generating tasks backward from a demonstrable failure of a defined invariant.**

This produces more meaningful difficulty than asking for harder code in the abstract.

---

# 4. Stage Three: Construct a Realistic Failure

This is the most important adaptation.

The task generator should identify an implementation strategy that appears reasonable but fails under a particular condition.

A strong task has three components:

**A plausible solution or existing implementation → a counterexample → a required correction.**

The counterexample should be executable, reproducible, and relevant to the engineering specification.

## Domain 1: Distributed Systems and Concurrency

**Primary difficulty:** Interacting guarantees, partial failures, race conditions, recovery, and temporal reasoning.

**Strongest mutation:** Introduce a realistic execution sequence that defeats a seemingly correct protocol.

Example world:

A three-node replicated key-value store with a leader, followers, write acknowledgments, and failover.

Initial task:

"Implement replication of client writes to follower nodes."

Harder task:

"Ensure acknowledged writes remain durable after leader failure under the specified replication and failover guarantees."

Adversarial execution:

```text
1. Client sends write(x = 10) to leader A.

2. A replicates x = 10 to follower B.

3. B acknowledges the write.

4. A acknowledges success to the client.

5. A crashes before another follower receives x = 10.

6. A stale follower becomes the new leader.

7. Client reads x.

8. Client receives the previous value.
```

The task must specify whether this failover sequence is legal under the intended protocol. The engineering challenge is to prevent an acknowledged write from being lost while retaining the requested availability and safety guarantees.

Potential difficulty mutations:

- Add delayed and reordered messages.
- Add network partitions.
- Allow concurrent reads and writes.
- Introduce leadership changes during replication.
- Add process restarts and durable logs.
- Introduce retries and duplicate messages.
- Combine recovery with membership changes.

The generator should use a deterministic scheduler or model checker to find and reproduce failing interleavings.

## Domain 2: Algorithmic and Constraint-Solving Problems

**Primary difficulty:** Global optimization, interacting constraints, invariants, and algorithms that work locally but fail globally.

**Strongest mutation:** Construct a counterexample to a plausible greedy or heuristic solution.

Example world:

A task scheduler assigning jobs to machines under deadlines and resource limits.

Initial task:

"Assign jobs to available machines without violating capacity constraints."

Harder task:

"Find the minimum-cost assignment while respecting machine eligibility, deadlines, resource consumption, and dependencies."

Adversarial instance:

```text
Jobs:
A: eligible for machine 1 or 2
B: eligible only for machine 1

Machines:
1: capacity 1
2: capacity 1
```

A greedy algorithm that assigns A to machine 1 first makes B impossible to schedule.

A globally valid assignment exists:

```text
A -> machine 2
B -> machine 1
```

Potential difficulty mutations:

- Increase interacting constraints.
- Add resource dependencies.
- Introduce weighted optimization objectives.
- Construct cases where locally optimal choices are globally invalid.
- Introduce adversarial graph structures.
- Require an optimality guarantee rather than any feasible solution.

Use exact algorithms, constraint solvers, or exhaustive search on small instances to establish ground truth.

## Domain 3: Performance and Resource Optimization

**Primary difficulty:** Meeting scalability constraints while preserving correctness.

**Strongest mutation:** Create a workload under which a straightforward correct implementation fundamentally cannot meet the resource requirement.

Example world:

A service processes millions of events and computes aggregate statistics over sliding windows.

Initial task:

"Compute the correct aggregates for the supplied event stream."

Harder task:

"Support the same semantics at a larger scale while remaining within a defined memory and latency budget."

The baseline might recompute every aggregate by scanning all historical events.

Its output is correct on small inputs.

However, the required workload makes the implementation too expensive.

Potential difficulty mutations:

- Increase input cardinality.
- Introduce skewed input distributions.
- Add strict memory limits.
- Add simultaneous queries and updates.
- Require amortized or worst-case performance bounds.
- Introduce cold-start, cache eviction, or streaming constraints.
- Combine throughput targets with exact correctness requirements.

The performance requirement must be measurable.

A good task specifies the input distribution, hardware or execution budget, warmup policy, relevant workload sizes, and correctness tolerance.

The generator should not make the task difficult solely by setting an arbitrary timeout.

## Domain 4: Database Consistency and Recovery

**Primary difficulty:** Isolation anomalies, transactional boundaries, crashes, concurrent updates, and persistent state.

**Strongest mutation:** Create a concurrent or failure execution that violates a business invariant despite individually successful operations.

Example world:

An inventory reservation system backed by a relational database.

Initial task:

"Reserve inventory when sufficient stock exists."

Harder task:

"Prevent overselling under concurrent requests and recover safely from crashes and retries."

Adversarial sequence:

```text
Initial stock = 1

Transaction A reads stock = 1.
Transaction B reads stock = 1.

A decides reservation is valid.
B decides reservation is valid.

A creates a reservation.
B creates a reservation.

Both transactions commit under an insufficient
isolation or locking strategy.

Confirmed reservations = 2.
Available stock = 1.
```

Potential difficulty mutations:

- Add concurrent transactions.
- Introduce write skew.
- Add idempotency keys.
- Introduce crashes between database commits and external effects.
- Add retry behavior.
- Introduce stale replicas.
- Add multi-table business invariants.
- Combine isolation requirements with throughput constraints.

The world should use a real database engine when engine-specific isolation and recovery semantics are part of the task.

## Domain 5: Compilers, Parsers, and Semantic Transformations

**Primary difficulty:** Preserving exact semantics across complex inputs and interacting language rules.

**Strongest mutation:** Construct an input on which an apparently valid transformation changes the program's behavior.

Example world:

A small compiler or source-to-source optimizer with parsing, intermediate representation, and optimization passes.

Initial task:

"Implement constant propagation."

Harder task:

"Implement constant propagation while preserving behavior across aliasing, branches, loops, and side effects."

Adversarial example:

```python
a = [1]
b = a

b[0] = 7

print(a[0])
```

A transformation that treats `a` as an independent immutable value could incorrectly replace the final expression with `1`.

The correct result is `7`.

Potential difficulty mutations:

- Add aliases.
- Introduce nested control flow.
- Add exception paths.
- Introduce mutable shared state.
- Add evaluation-order requirements.
- Introduce language-specific edge cases.
- Compose multiple optimization passes.
- Test interactions between parser state and incremental input.

Use interpreters, differential testing, AST checks, or other independently grounded semantic oracles.

## Domain 6: Cross-Component Debugging and Stateful Workflows

**Primary difficulty:** Causal diagnosis, hidden runtime state, module interactions, and preserving existing guarantees.

**Strongest mutation:** Make the failure emerge from several individually reasonable decisions across components.

Example world:

A web service with an API gateway, background worker, cache, database, and event queue.

Initial task:

"Fix an issue in which a background job can be processed twice."

Harder task:

"Prevent duplicate business effects when retries, worker restarts, cache invalidation, and delayed acknowledgments interact."

Adversarial sequence:

```text
1. API receives a request.

2. Request creates a job.

3. Worker processes the job.

4. Database operation commits.

5. Worker crashes before acknowledging the message.

6. Queue redelivers the job.

7. Replacement worker processes it again.

8. Duplicate business effect occurs.
```

Potential difficulty mutations:

- Add retry layers.
- Introduce stale caches.
- Add delayed event delivery.
- Introduce partial writes.
- Add process restarts.
- Add cross-service consistency requirements.
- Introduce misleading logs or symptoms.
- Require a fix that preserves existing behavior in other modules.

The solver must diagnose the failure across components and implement an appropriate correction rather than patching the visible symptom.

---

# 5. Stage Four: Create the Engineering Task

Once the world and failure have been established, the generator constructs the task that a coding agent will receive.

Each task should include:

| Component | Description |
|---|---|
| Repository | The code the solver can inspect and modify |
| User request | Natural-language engineering objective |
| Existing behavior | What currently works and what is defective |
| Correctness contract | Required invariants and externally visible behavior |
| Constraints | Compatibility, correctness, performance, and resource requirements |
| Available tools | Terminal, editor, test runner, debugger, etc. |
| Acceptance criteria | Conditions under which the solution is considered correct |
| Hidden verification | Tests and adversarial scenarios not directly disclosed to the solver |

The task must not reveal its reference implementation.

It also should not unnecessarily reveal the exact counterexample if discovering that counterexample is a meaningful part of the challenge.

However, the expected behavior must be sufficiently specified that the task is solvable without guessing arbitrary hidden requirements.

For example:

**Weak task:**

"Fix the concurrency issues in this database."

**Stronger task:**

"The reservation service occasionally confirms more units than are available when multiple clients reserve the same SKU concurrently. Modify the implementation to preserve the stock invariant under concurrent requests, retries, and the specified transaction isolation level. Preserve existing API behavior and ensure that retries cannot create duplicate confirmed reservations."

The second task supplies a precise engineering objective while leaving the diagnosis and implementation to the solver.

---

# 6. Stage Five: Generate a Reference Solution and Verifier

The synthesis agent should implement a correct reference patch.

This is the equivalent of Prime Intellect's gold tool-call trajectory, adapted to software engineering.

## Reference solution

The generator modifies the code until the specified properties hold.

The reference patch should:

- Compile successfully.
- Pass existing regression tests.
- Correct the targeted failure.
- Preserve documented behavior.
- Satisfy resource constraints where applicable.

## Independent verifier

The verifier should execute the candidate implementation and inspect its behavior.

```text
Candidate implementation
           |
           v
Build / typecheck
           |
           v
Existing regression tests
           |
           v
Adversarial failure reproduction
           |
           v
Invariant / semantic checks
           |
           v
Performance checks (when applicable)
           |
           v
Final acceptance result
```

### Required validation

```python
assert baseline_is_valid_for_existing_tests()

assert targeted_failure_exists_in_baseline()

assert reference_patch_applies_cleanly()

assert reference_patch_passes_existing_tests()

assert reference_patch_passes_adversarial_tests()

assert reference_patch_preserves_invariants()
```

For performance tasks, validate resource requirements separately.

For concurrency tasks, test multiple relevant schedules or use systematic schedule exploration where feasible.

For compilers, compare observable behavior across generated inputs.

The verifier should also be tested against deliberately incorrect implementations.

**A reference patch passing its own generated tests is not enough.** The test suite should distinguish meaningful solutions from partial fixes, hardcoded outputs, and incorrect shortcuts.

---

# 7. Stage Six: Evaluate Task Difficulty with a Coding Agent

After validating the task, give it to a separate coding agent.

The solver receives the repository, instructions, and ordinary development tools.

It should not receive the gold patch or private adversarial test cases.

For compatibility with the original methodology, a starting configuration is:

```text
Calibration attempts: 20
Solver: Target coding agent or representative strong model
Environment: Fresh repository state for each attempt
Scoring: Executable correctness verification
```

For each attempt:

```text
Reset repository
       |
       v
Provide task instruction
       |
       v
Solver inspects code
       |
       v
Solver edits files
       |
       v
Solver runs tests/debugger
       |
       v
Solver submits changes
       |
       v
Run independent evaluation
       |
       v
Record success or failure
```

Measure:

- Fraction of attempts that produce fully correct patches.
- Whether the original failure is fixed.
- Whether existing behavior is preserved.
- Whether resource limits are satisfied.
- Whether the agent introduces new bugs.
- Which technical invariants are most frequently violated.

This produces an empirical estimate of task difficulty.

The calibration should use a coding agent with capabilities representative of the models we intend to train.

Otherwise the resulting difficulty levels may be poorly calibrated.

---

# 8. Stage Seven: Evolve the Task Through Controlled Mutations

Preserve the original five-tier structure as a starting point.

| Tier | Target solver success | Desired task properties |
|---|---:|---|
| t0 | 80–100% | One localized bug or straightforward implementation |
| t1 | 60–80% | Multiple interacting requirements |
| t2 | 40–60% | Nonlocal interactions or counterexamples to simple solutions |
| t3 | 20–40% | Adversarial execution, global invariants, or multiple failure modes |
| t4 | 0–20% | Complex interactions that defeat plausible high-quality implementations |

These targets are initial calibration settings, not proof that a task is intrinsically difficult.

### Mutation operators

The original General Agent strategies remain useful, but should be supplemented with software-engineering-specific operators.

| Mutation operator | Engineering effect |
|---|---|
| Add concurrent execution | Introduce interleavings and shared-state races |
| Inject partial failure | Add crashes, timeouts, or lost messages |
| Introduce global invariants | Make independently valid actions interact |
| Generate a counterexample | Defeat a plausible greedy, heuristic, or local fix |
| Tighten resource bounds | Force a different algorithm or architecture |
| Add persistent state | Introduce recovery and replay requirements |
| Extend cross-component dependencies | Make the defect span modules or services |
| Add semantic edge cases | Break transformations under aliasing or control flow |
| Add realistic distractors | Introduce plausible but incorrect diagnoses |
| Compose failure modes | Combine two independently challenging conditions |
| Expand state space | Add entities or inputs that materially affect behavior |
| Introduce temporal constraints | Require reasoning about operation order and timing |

A good mutation should satisfy three conditions:

**1. It increases the technical reasoning required.**

**2. The mutated task remains solvable and independently verifiable.**

**3. Its added complexity is causally relevant to the solution.**

For example, adding 20 unrelated source files does not make a concurrency task meaningfully harder.

Adding a new retry path that changes which operations can execute twice does.

---

# 9. Stage Eight: Use Solver Failures to Guide the Next Mutation

The generator should not mutate blindly.

After testing a task, inspect the failed solver attempts.

Identify why the solver failed:

```text
Did it misunderstand an invariant?

Did it choose a locally valid but globally invalid fix?

Did it overlook a concurrent execution?

Did it assume an operation was atomic?

Did it ignore persistent state?

Did it introduce a performance regression?

Did it fail to inspect another component?

Did it overfit to the visible test cases?
```

Then use that information to generate a more focused next version.

Example:

```text
Current task:
Fix duplicate job processing.

Solver behavior:
Adds a process-local set of processed job IDs.

Observed result:
Passes single-process tests.

Failure:
Duplicates return after worker restart.

Next mutation:
Add persistent recovery and a restart sequence
that invalidates the process-local solution.
```

This is a particularly effective evolution mechanism because it creates tasks that target demonstrated model weaknesses.

The generator should preferentially evolve tasks around **plausible incorrect fixes**, not arbitrary additional requirements.

It can also select from a library of counterexample strategies corresponding to the six engineering domains.

---

# 10. Full Generation Loop

The following pseudocode represents the proposed adapted system.

```python
DIFFICULTY_BANDS = [
    (0.80, 1.00),
    (0.60, 0.80),
    (0.40, 0.60),
    (0.20, 0.40),
    (0.00, 0.20),
]


def generate_engineering_family(domain):
    # 1. Build an executable software world.
    world = generator.build_world(domain)

    # 2. Define required correctness properties.
    invariants = generator.define_invariants(world)

    # 3. Identify a meaningful failure mode.
    failure = generator.construct_failure(
        world,
        invariants,
    )

    # 4. Generate the initial engineering task.
    task = generator.create_task(
        world,
        failure,
        invariants,
    )

    accepted = []

    for tier, target_band in enumerate(DIFFICULTY_BANDS):

        for revision in range(MAX_REVISIONS):

            # Generate a known-correct reference patch.
            gold_patch = generator.solve(task)

            # Build/extend an independent verifier.
            verifier = build_verifier(
                task,
                invariants,
                failure,
            )

            # Validate the task and reference patch.
            validation = validate_task(
                world,
                task,
                gold_patch,
                verifier,
            )

            if not validation.passed:
                task = generator.repair_task(
                    task,
                    validation.feedback,
                )
                continue

            # Challenge the solver repeatedly.
            results = run_coding_agent(
                task=task,
                attempts=20,
                verifier=verifier,
                reset_between_attempts=True,
            )

            pass_rate = compute_pass_rate(results)

            if within_band(pass_rate, target_band):
                accepted.append({
                    "tier": tier,
                    "task": task,
                    "world": world,
                    "gold_patch": gold_patch,
                    "verifier": verifier,
                    "pass_rate": pass_rate,
                    "failure": failure,
                })
                break

            # Use solver traces to improve difficulty.
            feedback = analyze_solver_failures(results)

            task, world, failure = generator.mutate(
                task=task,
                world=world,
                failure=failure,
                feedback=feedback,
                target_band=target_band,
            )

        else:
            # No valid task found at this tier.
            # Do not silently treat it as accepted.
            break

        if tier < 4:
            task, world, failure = generator.evolve(
                accepted[-1],
                next_tier=tier + 1,
            )

    return accepted
```

This is architectural pseudocode, not a drop-in production implementation.

In practice, the world generator, reference solver, verifier auditor, and calibration solver can be separate agents or services.

The generator should retain a history of mutations and solver failures so successful strategies can be reused.

---

# 11. What to Preserve and What to Change from Prime Intellect

| Component | Preserve or adapt? | Our approach |
|---|---|---|
| World-first generation | Preserve | Generate an executable repository and runtime |
| LLM as synthesis agent | Preserve | Agent can inspect, edit, compile, execute, and debug code |
| Gold reference solution | Preserve | Generate a correct patch or implementation |
| Executable verification | Preserve and strengthen | Independent tests, invariants, adversarial workloads |
| Difficulty calibration | Preserve | Repeated attempts by a strong coding agent |
| Five difficulty tiers | Preserve initially | Calibrate against actual coding performance |
| Iterative mutation | Preserve | Mutate technical failure conditions |
| Larger databases | Adapt | Increase meaningful state-space complexity |
| Tool proliferation | Use selectively | Prefer relevant module and interface interactions |
| Noisy instructions | Deprioritize | Real engineering ambiguity is preferable to artificial typos |
| Ambiguity resolution | Strengthen | Misleading symptoms and plausible incorrect diagnoses |
| Cross-entity coupling | Strengthen | Cross-module, transactional, and concurrent dependencies |
| Failure-mode targeting | Add | Mutations defeat plausible incorrect implementations |
| Counterexample generation | Add | Build minimal executable cases that expose incorrect logic |
| Solver-failure analysis | Strengthen | Use incorrect patches to select subsequent mutations |

The biggest shift is that **task difficulty should be rooted in software behavior rather than task presentation**.

---

# 12. Recommended Quality Criteria

Before accepting a generated task, ask:

**Is the engineering problem real?**

There must be a meaningful defect or implementation requirement, not an arbitrary puzzle disguised as software engineering.

**Is the failure reproducible?**

The task should have a deterministic reproduction or sufficiently reliable, controlled evaluation procedure.

**Does a correct solution exist?**

The reference implementation must demonstrate feasibility.

**Does the verifier distinguish correct from incorrect solutions?**

It should catch plausible but flawed fixes, not merely check a happy path.

**Does the task require meaningful reasoning?**

The hard part should be a technical dependency, invariant, execution sequence, or algorithmic property.

**Is the difficulty relevant?**

Extra files, larger prompts, and irrelevant operations should not be rewarded as difficulty.

**Is the target model genuinely challenged?**

Repeated solver evaluations should establish that the task exposes a capability gap.

**Does the task generalize beyond the exact test case?**

A valid solution should implement the specified behavior, not simply recognize the generator's hidden example.

---

# 13. Final Design Principle

The core approach should be:

**First create a system that can behave incorrectly in an interesting way. Then construct a task that requires understanding and correcting that behavior. Finally, search for increasingly subtle conditions under which plausible solutions still fail.**

For the six software engineering domains, the strongest sources of difficulty are:

| Domain | Primary difficulty generator |
|---|---|
| Distributed systems | Adversarial execution schedules and partial failures |
| Algorithms | Counterexamples to locally plausible solutions |
| Performance | Correct implementations that violate scalability requirements |
| Databases | Concurrent transactions or failures that break persistent invariants |
| Compilers | Inputs that expose semantic changes in apparently valid transformations |
| Cross-component debugging | Multi-step causal failures across stateful modules |

The most promising automated process is therefore:

**Build a realistic software world → identify its invariants → produce an executable counterexample → turn it into an engineering task → generate and validate a reference fix → challenge a coding agent → analyze incorrect solutions → evolve the task to defeat those solutions.**

That is the specific part of Prime Intellect's approach worth preserving, with a new emphasis on **counterexample-driven software engineering task evolution**.
