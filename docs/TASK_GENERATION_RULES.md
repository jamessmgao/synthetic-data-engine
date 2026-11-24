# Task Generation Rules

How a task is authored from a world (agent mode, as built), what the author must and must
not write, how the pipeline enforces it, and the difficulty and diversity levers behind it.
The world itself is docs/WORLD_CONSTRUCTION.md; the gates are docs/VALIDATION.md.

## The task author (agent mode)

One authoring trial per `create` / `revise` job (`generator/author_task.py:run_create_job`):
terminus-2 + GLM-5.1 in the authoring image, with the instruction rendered from
`generator/prompts/task_author.md` (`render_task_author_instruction`). Its slots: the world
summary (`world_summary`: family, mechanism, the **numbered** invariants, the hidden
counterexample, the plausible wrong fix), the shape and dial (`shape_section`), the pipeline's
rules (`_system_rules`: `generator/prompts/system.md` with the pinned base images, minus its
one-shot "Return exactly one JSON object…" paragraph, since here the draft is a file), and
the request (`revise_request`: initial, repair, easier, repair_ambiguity).

**Budget:** the prompt asks for about 10 minutes. The hard limit is a 2-hour safety net
(`SDE_CREATE_TIMEOUT_S`, default 7200 since `66f3046`). The task's repository is the world's `environment/` as it is: the
author does not grow, rewrite or add modules.

### The instruction: guarantees and interface, nothing that solves it

The instruction states the symptom users saw, the guarantees that must hold (the invariants,
as observable outcomes), everything that must keep working, and the interface (every value,
path, command and format the tests rely on). It must **not** state:

- the counterexample: its steps, its schedule, the input or time step that triggers it;
- where a crash or fault fires relative to the system's effects;
- crash hooks or stage names: it says only that workers can be killed at any moment and are
  restarted; the tests kill them from outside;
- the cause, the files to change, or the shape of the fix (in any words);
- the checks a command applies, or the criteria an answer meets: say what it must report;
- the symptom with a when / if / because / for-example clause (BAD: "fails when one macro calls
  another"): only what users saw;
- for a speedup, the orderings, tie-breaks or edge cases to keep: say "the same output as
  now".

From P1 (`tests/fixtures/p1-local-1`, quoted in the prompt):

> BAD: The documented fault-injection hook is `STOP_AFTER=inventory`: when a claimed request
> can be fulfilled, a worker exits with status `70` after the accepted inventory movement and
> stock update are durable and before the worker's remaining completion work. Use
> `tick --milliseconds 30001` to expire a lease. … Once a request ID has produced an inventory
> movement, any later delivery of that same request ID must reuse that exact durable result
> without applying another inventory effect.

> GOOD: A worker process can be killed at any moment (including in the middle of a write) and
> a new one started. Time advances only through `tick --milliseconds N`; a lease lasts
> 30000 ms. … Each request ID consumes its submitted quantity at most once, whatever the
> retries, crashes and lease expiries; stock never goes negative; and a request that fits the
> stock is eventually fulfilled.

The leak gate enforces this semantically, paraphrases included (docs/VALIDATION.md §2).

### The hidden tests: invariants as properties over many executions

1. **One dedicated test per world invariant**, named after it, marked `# invariant: N`
   (N = its number in the world summary). Its checker reads only observable state (command
   output, inspect / report JSON, files the interface exposes) and asserts the invariant
   **after every step** of **at least 10 fixed seeds** of randomized schedules: random
   operations, the process killed (SIGKILL from the test) at a random step and restarted,
   retries, lease expiries, concurrent workers where the world allows them.
2. **The exact counterexample** as one case among them, replayed as `repro.sh` does it (from
   the image's `WORKDIR`, by absolute `/app/...` paths), so it fails on the world exactly as
   the world check saw.
3. **The dependent**, if the world has one: a test of that module's output, so a one-file
   patch fails.
4. **Regression and edge cases** for everything that must keep working (normal path, empty,
   boundary, invalid and largest-allowed inputs), with **exact expected values**.
5. Together they reject the wrong fix for a real reason, and nothing passes on a stub.

Tests drive the system only through its interface, never by importing internals, and keep
the suite under 60 s on 1 CPU. Each test starts from clean state it creates (a fresh data dir,
DB and state files; a service on a free port it picks, or after stopping a leftover listener),
because the agent's experiments leave files, rows and servers behind. Every blocking read of a
child process has a timeout, and processes run one at a time unless the instruction states the
overlap. Seeds are fixed: `rng = random.Random(seed)` for
`seed in range(10)`, never the global `random.*` unseeded, `random.Random()`, or a clock seed.

### The other draft fields

- `dockerfile`, `environment_files`: the world's `environment/` with the defect, without
  `repro/`, `world_fix/`, `world.json` or anything that walks through the failure.
- `solution_script` (+ `solution_files`): a genuine fix, from `world_fix/fix.sh`.
- `wrong_fix_script`: the world's `plausible_wrong_fix`, implemented as an engineer would,
  whose first line is `# plausible_wrong_fix: <the text from world.json>`.
- `spec`: `correctness_constraints` (the invariants and must-keep-working behaviors) and
  `coverage` (each mapped to its tests).

### Writing files

One heredoc per file, or a short Python script in `/tmp` that writes them (and `json.dump`s
`draft.json`). **Never nested heredocs sharing a delimiter**: the inner `EOF` ends the outer
one (P1 lost ~3 minutes to it).

### The self-check: `sde-task` (`authoring/sde_task.py`)

```
sde-task schema                                     # the TaskDraft JSON schema: run it first
sde-task check /logs/artifacts/draft.json           # {ok, errors}: every pipeline rule
sde-task run /logs/artifacts/draft.json --as all    # oracle reward 1; nop 0; wrong 0
```

- `check` (`check_draft`) runs every deterministic `TaskDraft` check plus the ties to the
  world: the wrong fix quotes world.json's `plausible_wrong_fix`; every invariant N has a
  `# invariant: N` marker in `test_outputs` or a `.py` support file (markers naming no
  invariant are errors); randomized tests use fixed seeds (`unseeded_randomness`).
- `run` (`run_roles`, `run_bundle`) builds `environment/` **once** and runs each role in a
  fresh container capped like the evaluation (1 CPU / 2 GB; the role's script as `agent`,
  the tests as root), and reports every test with its status and message, plus
  `setup_errors` (a nop / wrong run failing only on an import error, a missing file or no
  reward does not count). Inside a trial the image is tagged by the content of
  `environment/` and kept, so later runs skip the build.
- **The flake check**: `--as all` runs the oracle a second time on the same image; different
  per-test results or reward fail it as flaky (`result.repeat`).
- Results are saved under `/logs/artifacts/sde_task/` (`check.json`, `run-<role>.json`); the
  create job's guard (`generator/self_check.py:ensure_task_checked`) reads them, or runs the
  check itself. A failing check is a FAILED / author create, sent to a repair.

## Taxonomy v2 (`shared/taxonomy/taxonomy.py`)

A spec is `{family, mechanism, shape, environment, verification, dial, domain, domain_label,
mechanism_note, seed?, design?}`. Six families (`FAMILIES`), each with its mechanisms,
environments and verification methods (GET /v1/families lists them; the table is in
docs/AUTHORING.md §3.2). `domain` is one of about 40 application domains (`APP_DOMAINS`;
performance never draws `payment_ledger`), and `mechanism_note` (`MECHANISM_NOTES`) is the
defect pattern the world author plants. Retired on 2026-11-18: `greedy_counterexample` and
`optimality_required` (the honest guarantee names the fix), `scalability_wall` and
`memory_bound` (generic). `shape_for(mechanism)` gives fix / implement / optimize /
migrate (every performance mechanism is `optimize`); the dial is 1-3 (`DEFAULT_DIAL` 2). `legacy_view` gives the v1
`{taxonomy, difficulty}` view one-shot mode uses.

`sample_spec_v2(existing, area, mechanism, environment, dial)` spreads a batch: the family
with the least weighted use (uses / `FAMILY_WEIGHTS`) first, so a weight-2 family is picked
about twice as often; within it an unused (mechanism, environment) combination with the
least-used values, then its least-used verification method. A family repeats a combination
only after using all of its own. `area` limits it to one family or a list (a 3-task batch
over 3 families gets one task per family).

Each family's world brief (`generator/families.py:FAMILY_BRIEFS`) names its difficulty
mechanisms, strongest lever, typical worlds, how to build the counterexample, the scaffolds
that fit, and why a local patch fails there.

## Difficulty controls (background)

Two kinds of levers: **quantitative** levers, which code can measure, and **semantic** levers,
which the generator controls.

| Quantitative lever | Example |
|---|---|
| Input size | 10 files vs. 100 files |
| Component count | 1 module vs. 4 interacting modules |
| Constraint count | Correctness only vs. correctness + ordering + idempotency |
| Required scenarios | Normal execution vs. normal execution + crash recovery |
| Dependency depth | 1 sequential subgoal vs. 3 dependent subgoals |

| Semantic lever | Easier | Harder |
|---|---|---|
| Diagnosis difficulty | Bug identified in instructions | Agent must discover root cause |
| Constraint interaction | Requirements independent | Fixing one can break another |
| Information discovery | Relevant logs provided directly | Agent must determine which evidence matters |
| Failure complexity | Happy-path behavior | Concurrency, partial failures, recovery |
| Planning complexity | One obvious implementation step | Multiple dependent decisions |

**Constraint interaction** and **diagnosis difficulty** matter most; size alone does not make
a task hard (the pilots: readable defects in large codebases were solved anyway). That is
why worlds are capped at 300-800 lines and must make a local fix insufficient, and why the
instruction may not do the diagnosis for the agent. Decision-point counts are a generation
target only: the trajectories (the Trace Parser's review) show whether they mattered.
