# Evaluation: the Executor, the Trace Parser, mutations and verdicts

How a built task version is measured and decided: the code in `evaluation/`, as built
on main. The band is fixed: a version is **learnable** when 1–3 of 5
`terminus-2` + `glm-5.3-flash` (`reasoning_effort=high`) runs pass all tests, and the
failures are the model's, not the task's.

## 1. Executor: five agent runs per version

`evaluation/executor.py:Executor` claims `rollout` jobs (`<task>-v<n>-rollout<slot>-r<retry>`)
and runs one Harbor trial (one agent run) each (`evaluation/harbor.py:run_trial`, pinned agent
`evaluation/harbor.py:pinned_agent`, from `shared/contracts/config.py` `EVAL_*`).

- **Idempotent per job id.** The trial directory lives under `traces/<job_id>/`. A
  finished trial left by a dead worker is adopted instead of re-run (`finished_trial`).
  A trial cancelled at shutdown is set aside and re-run, not adopted. An orphaned Harbor
  process (same host, verified pid) is waited for and adopted. The liveness check reads
  `/proc/<pid>/cmdline` (`_command_line`), because the production image has no `ps`:
  a reclaimed agent run used to settle INVALID with `FileNotFoundError: 'ps'` (fixed in
  `adfcbf2`).
- **Cross-machine.** Before a run, `artifacts.fetch_trace(job_id)` brings a trial
  another machine finished, or `abandon_trace` sets a half-upload aside. After the run
  `save_trace` uploads it with a `.complete` marker.
- **Heartbeats survive store errors** (`Executor._heartbeat`). An exception, for example
  RDS out of connections, is logged and retried every 5 s. Only a *refused* heartbeat
  (another owner holds the lease) cancels the trial. Settle is token-fenced either way.
  Before `60e561b`, the exception killed the heartbeat thread, the lease lapsed under a
  running trial, and another EC2 instance re-ran it (seen live on AWS, archive/ANALYSIS.md §11).
- **Classification** (`evaluation/classify.py:classify`):
  - `VALID_PASS` or `VALID_FAIL`;
  - `TIMEOUT`: the agent hit its 1-hour limit with reward 0. The run waits for the Trace
    Parser's timeout review (§2), which counts it as a model failure or sends it back as
    INVALID;
  - `INVALID`: a Harbor or environment exception, a missing or non-binary reward, or an
    agent config that isn't the pinned one.
  
  An INVALID run is never scored; its slot is retried up to `MAX_ROLLOUT_RETRIES` (3), then
  the task is `REJECTED infra_unresolved`, unless the scored runs already make the verdict
  certain (early stopping, §2). An agent that closes its own terminal after the
  verifier scored is scored, not INVALID. A breaker (`_Breaker`: `BREAKER_INVALID` INVALIDs in
  `BREAKER_WINDOW_SEC`) pauses claims, so an outage doesn't burn every task's retries.
- **Settle** records outcome, reward, cost and trace URI, and pings the Trace Parser
  with an `<id>-analyze` job in the same transaction. A cancelled job's settle is fenced,
  but its cost is kept (tb3-cancel-path).
- **Shutdown.** On a deploy or KEDA scale-in, the executor drains: running agent runs finish,
  for up to `SDE_DRAIN_ON_TERM_S` (3700 s in aws-prod). A Spot interruption hands jobs back at
  once. Production runs KEDA 2–16 executor pods × 2 agent runs, each pod requesting 500m CPU
  (`deploy/k8s/overlays/aws-prod`).

## 2. Trace Parser: analyze → accept / mutate / ease / repair / reject

`evaluation/trace_parser_service.py:TraceParserService.analyze` runs on every analyze ping.
Until the version's verdict is decided, it only waits (or queues a retry for an INVALID
slot). The verdict is decided when all 5 slots are scored, or earlier by **early stopping**
(`early_stop`):
- with p passes and r slots left, p ≥ 1 and p + r ≤ 3 is certainly in band;
- p ≥ 4 is certainly too easy;
- it never stops on too few passes.

The remaining runs are closed as `SKIPPED`. Then:

1. **Parse every scored trace** (`evaluation/trace_parser.py:parse`): deterministic
   `trace_metrics` (agent steps, tokens, tool calls, files modified, duration, declared
   completion) and, for a failed run, the failing tests and evidence lines (`E1…`).
2. **Failure review** (one glm-5.1 call per failed run): explains the failure citing
   evidence ids. It's attributed to the **model** or the **task**, else `unexplained`.
   A run the review blames on the task means the task, not the model, failed.
   **Timeout review:** a `TIMEOUT` run gets one more call, which reads the trace's last
   stretch (an unchanged screen, the largest command, repetition). A model failure (a wedged
   terminal, a loop, its own code hung) becomes `VALID_FAIL`; anything else becomes
   `INVALID` and is retried. Examples: [VALIDATION.md](VALIDATION.md) §3.
3. **Pass review** (`trace_parser.review_passes`; one glm-5.1 call per too-easy version,
   over its fastest passing runs): `why_easy`, `simple_strategy`, `cues` (instruction or
   code text that pointed at the fix), `fix_applied`, `suggestions[].technique`,
   `cited_evidence` and `trusted` (false when it cites nothing real). It never raises; a
   failure is recorded as `review_error`.
4. **Decide** (`evaluation/calibration.py`):

| Outcome | Condition | Next job (`next_job`) |
|---|---|---|
| accept | 1–3 valid passes and no run blamed on the task | none: ACCEPTED, published with a verdict |
| reject | out of band at `MAX_VERSIONS` (8) | none: REJECTED exhausted |
| too easy | 4–5 passes | agent mode with a world: `world` job, origin `mutation` (`<t>-v<n+1>-world`); otherwise a one-shot `revise` (origin `revision`) |
| too hard | 0 passes | agent: `revise` origin `easier` on the same world; else one-shot `revise` |
| task blamed | a failure attributed to the task | `revise` origin `repair_ambiguity` |

The decision and the version bump commit in one compare-and-set on the task (a second
copy deciding the same version records `stale`). The analyze job's `result` holds the
action, summary, pass count, `direction`, `easy_analysis` (the pass review) and
`mutation`.

## 3. Mutation rules (`calibration.choose_mutations`)

Deterministic, no model. Up to `MAX_MUTATIONS` (2) operators from `MUTATION_OPERATORS`
(WORLD_CONSTRUCTION.md §8 plus `remove_cues`), chosen from the evidence. Operators an
earlier version already applied go last. Every choice records `{operator, rule, evidence}`
in `result.mutation.rules` and in the mutation history, so the rules can be measured.

| Rule | Fires on | Operators |
|---|---|---|
| `cues_found` | the trusted pass review quotes cues (the task leaks) | `remove_cues`, **always first** |
| `process_local_guard` | the fix kept a guard in one process (review phrases, or code such as `= set()`) | `add_persistent_state`, `inject_partial_failure` |
| `local_reorder_or_serialize` | a local reorder or lock passed (`threading.Lock(`, "lock", "reorder") | `add_concurrent_execution`, `compose_failure_modes` |
| `self_checking_agent` | the agents fuzzed or tested their own fix | `introduce_global_invariant`, `generate_counterexample` |
| `locally_plausible_strategy` | a greedy, heuristic or sort-based strategy passed | `generate_counterexample` |
| `timing_dependent` | the fix leaned on retries, sleeps or timeouts | `introduce_temporal_constraint` |
| `special_case` | a special case or hard-coded value passed | `add_semantic_edge_case` |
| `one_file_patch` | passing runs edited a median of ≤1 file | `extend_cross_component_dependency` |
| `straight_to_the_fix` | a median of ≤8 agent steps and no cues | `add_realistic_distractor` |
| `pass_review_suggestion` | the review's `suggestions[].technique` (`TECHNIQUE_OPERATORS`), tiebreak only | mapped operator |
| `default` | slots still open | `introduce_global_invariant`, then `generate_counterexample` |

Text rules match only the review's `fix_applied` / `simple_strategy`. Commands match
only specific code patterns, so ordinary words in shell commands don't fire. Too hard
and task-blamed versions get no operators; `target_fix` is the failing fix's explanation.

The mutation history travels in job inputs (`mutations: [...]`), so nothing shared is
rewritten. The world author writes it into the new world's `world.json` lineage.

`add_persistent_state` is a last resort (`LAST_RESORT`): it's chosen only when nothing else
fits, because none of its mutations reached the band.

The parent world's `world_check` hardness result goes into the mutation input
(`trace_parser_service`), and `world_mutation.md` renders it. Using it as a tiebreak in
`choose_mutations` is still parked (branch `park/hardness-tiebreak`, docs/TODO.md).

Reuse **variants** are world jobs with origin `variant`, created at submission (reseed +
replace; [AUTHORING.md](AUTHORING.md)), not by the Trace Parser.

## 4. Verdicts and reports

`evaluation/verdict.py:render` writes `verdict.md` for every accepted task:
- the decision;
- the spec;
- a versions table: origin (the version's first non-repair world/create/revise job,
  `shared/contracts/decision.py:version_input`), operator, mutation operators, world (sha and world_check
  outcome), bundle, gates, e.g. `OK (wrong_fix ✓, leak ✗)`, and pass pattern;
- the world mutation lineage, with why and aimed-at;
- every agent run with its trace URI;
- why each failed run failed;
- the cost.

`verdict.publish` copies the bundle and verdict to `$SDE_ACCEPTED_DIR/<task>/`. The
reconciler re-publishes anything missed. The API's report (`api/service.py:report`) and
version view use the same `version_input`.

## 5. Tests

`evaluation/test_executor.py`, `evaluation/test_harbor.py`, `evaluation/test_trace_parser.py`
and `tests/shared/jobs/test_services.py` (the Trace Parser service, the mutation rules, the
verdict) run on SQLite, Postgres and Postgres+Redis. The end-to-end behavior is in
docs/TESTING.md.
