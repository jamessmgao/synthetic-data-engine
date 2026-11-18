# What makes a task hard

Why some tasks sit at a model's capability frontier and others don't, and
how the generator aims for the frontier. The first half is general theory;
the second half is what we measured against `terminus-2` + `glm-5.3-flash`
(`reasoning_effort=high`) in pilots p1–p3 (`docs/PILOT_RESULTS.md`). Where the
two disagree, the measurements win.

The target is the **frontier regime**: 1–3 passes out of 5.

## 1. Seven sources of difficulty

| Source | Why LLM agents struggle | Example |
|---|---|---|
| 1. Combinatorial search | Too many candidate solutions to consider | An optimal schedule under 15 interacting constraints |
| 2. Long-horizon planning | Early mistakes propagate into later steps | A 30-step deployment workflow |
| 3. Constraint interaction | Satisfying one requirement can violate another | Change code without changing existing behavior |
| 4. State tracking | An accurate picture of changing information must be maintained | Inventory across many transactions |
| 5. Unfamiliar reasoning | Memorized patterns and heuristics stop helping | A novel logic puzzle with custom rules |
| 6. Partial observability | The needed information is hidden, missing or spread out | Debugging from incomplete logs with limited tools |
| 7. Error detection and recovery | The model must notice its own mistake and change course | Diagnose a failed request, repair the config, retry safely |

They don't contribute equally. Three dominate.

### A. Interacting constraints

*Easy:* schedule five meetings. *Hard:* schedule five meetings so everyone
is available, nobody attends overlapping meetings, some meetings happen in a
set order, two need the same room, and total waiting time is minimal.

The hard version is hard because the constraints **interact**: a model
satisfies four and quietly violates the fifth. More constraints don't
automatically mean harder. Some constraints shrink the search space and
make the answer obvious. Difficulty rises when constraints interact in ways
that need real reasoning or backtracking.

### B. Large search spaces with weak guidance

A task can be hard with perfectly clear instructions: a shortest route
through 20 locations with pickup-and-delivery constraints has many orderings,
and the locally best choice doesn't give the best overall result. LLMs can
reason through search, but they don't automatically search exhaustively or
keep a flawless symbolic record of every candidate. Problems that need
systematic exploration, rather than a plausible next step, are hard.

### C. Errors that compound across steps

Read the repository → find the bug → change the right module → update
dependent code → run tests → diagnose failures → fix regressions → finish.
Each step depends on the ones before. An agent good at every step can still
be unreliable at the whole chain. Verification and recovery counteract this,
**but only when the model notices that something went wrong.**

## 2. Harder doesn't mean longer

- **Summarize a 100-page report:** looks expensive; extracting the main
  themes is usually enough, and small omissions are tolerated.
- **Find the one subtle contradiction in a 10-page report:** two distant
  statements must be connected; missing one detail is failure.
- **Change one function without breaking anything:** three lines of code, but
  success needs the hidden dependencies and edge cases.

What matters is the **precision of reasoning required**, not the amount of
work. A long, repetitive task can be easy; a short task that needs one
non-obvious insight can be very hard.

## 3. Three difficulty regimes

| Regime | What it looks like |
|---|---|
| Easy | A familiar strategy works and small mistakes rarely matter |
| **Frontier** (the target) | The model has the needed capabilities but must apply them consistently across several interacting decisions. Some attempts find a correct path; others don't |
| Beyond capability | A capability, a crucial piece of information, or a way to search or verify is missing; nearly every attempt fails |

**Tools and time are part of difficulty.** A scheduling problem that is hard
in pure natural-language reasoning can become easy when the agent can write
and run an optimization program. Our agent has a shell, an hour, and
whatever the image installs. So "large search space" alone is weak against
an agent that writes a brute-force or ILP solver.

## 4. A working model: D = f(B, C, H, U, V)

*(The source this section is based on was cut off before defining the
variables. These are our reading, and the definitions we use.)*

| Variable | Meaning | Raises difficulty when |
|---|---|---|
| **B**: branching / search | how many plausible paths or candidate solutions exist | many candidates, weak local signal, and no cheap tool shortcut |
| **C**: constraint coupling | how strongly requirements interact | fixing one requirement breaks another |
| **H**: horizon | how many dependent steps must all be right | a single early mistake invalidates the end result |
| **U**: unfamiliarity | how far from memorized patterns the task is | custom rules, a domain-specific invariant, not a textbook bug |
| **V**: verifiability (inverse) | how well the agent can check its own work | the agent's own tests pass while a hidden requirement fails |

The frontier sits where the task is solvable (all information is stated, the
tools exist) but several of these are high at once, so success depends on
getting every interacting decision right.

## 5. What we measured with glm-5.3-flash

### It is very good at

- **Read the code, find the local bug, patch it.** 9/9 generated debugging
  tasks (easy, medium and hard as labeled) were solved 4–5/5, often in
  6–11 steps and 1–2 minutes. Typical planted bugs: `total = x` for
  `total += x`, `max()` for `sum()`, acknowledge-before-write.
- **Textbook patterns, with or without hints.** The ack-before-write job
  queue scored 5/5 as written, 5/5 with every hint removed, and 4/4 with an
  interacting constraint added; tokens per run tripled but it never failed.
- **Building and wiring things.** A fragmented-I/O protocol server (4/4),
  SQLite data forensics (4/4), and a 2M-line log tool under a time budget
  (5/5).
- **Verifying and recovering (source 7).** In the performance task two runs
  took 40–60 steps: the agent's own cleanup deleted a module and
  introduced an empty-input bug, and it found and fixed both, then passed. It
  writes its own differential tests, checks hash-seed stability, and
  benchmarks. With an hour, compounding errors (C) get caught.

### What made tasks easy (generator failure modes)

- **The level knobs add bugs, not difficulty.** Asking for more files,
  constraints, modules or subgoals gives more bugs of the same trivial kind.
  Raising quantity does not raise U or V.
- **The fix leaks through the requirements.** Guarantees written in terms of
  internals ("the check must compare against totals computed independently
  from raw events", naming the `carry_over` field) turn the instruction into
  a recipe. C and U collapse.
- **Hints:** "Key Files to Investigate", "Approach" sections, `# BUG:`
  comments. Now rejected by the schema.

### What looked hard but was a task defect

- `configure-git-webserver` (0/5) failed because its hidden verifier logs in
  as `git` with password `password`, which the instruction never states;
  flash had built and tested the system itself. **Unstated requirements fake
  difficulty**, and oracle + nop gates cannot detect them.
- A Go task failed partly because `go` was not on the agent's `PATH`.
- Our rule: a failure only counts when it is the model's fault, with the
  evidence read from the trajectory.

### The authoring ceiling

The harder the brief, the less often glm-5.1 can author it correctly: 6 of 8
hard briefs failed their own reference solution on the first try (3 were
compile errors), and 2 stayed broken after two repairs. Hard tasks cost
repairs; the generator must be able to *solve* what it asks for.

## 6. Implications for generating frontier tasks

1. **Aim at low verifiability (V) with everything stated.** flash recovers
   from any mistake it can see. It fails when its own checks pass but a
   precisely stated requirement doesn't. The best example is a long list of
   small, interacting rules checked against a hidden reference model on
   generated inputs.
2. **Couple the constraints (C).** "Change X without changing anything else"
   with a hidden regression suite generated from the original behavior;
   rules whose obvious implementations conflict.
3. **Use unfamiliar rules (U), not unfamiliar trivia.** Custom semantics
   (a small DSL, a ledger with specific correction rules) stated in full,
   instead of textbook bugs flash has seen a thousand times.
4. **Keep search hard after tools.** If a solver trivializes it, require
   exactness or optimality on instances where greedy fails and brute force
   is too slow, or move the difficulty to C/U/V.
5. **Give difficulty a dial.** The number of interacting rules, or the size
   of the hidden regression surface, can be turned up or down toward 1–3/5.
   The fixed easy/medium/hard labels could not.
6. **Never fake it.** No unstated requirements, no hidden credentials, no
   environment traps, no difficulty from running out the clock (timeouts
   are not model failures).
7. **Check failures by reading trajectories.** A task is in band only if its
   failures are the model's own reasoning errors.

## 7. Next experiments

- Pilot p2, s5 *many-rules*: implement against 14–18 interacting numbered
  rules; hidden reference model on seeded inputs (targets C, U, V).
- Pilot p4 *frontier*: briefs built from sections 1–6: behavior-preserving
  change with a hidden regression suite, a custom-semantics interpreter,
  optimal scheduling where greedy fails, a ledger with corrections and
  reversals, incident diagnosis from partial logs, and a chained migration.
