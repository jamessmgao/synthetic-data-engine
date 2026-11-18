# Iteration log (overnight tuning on AWS production)

The method: [HILLCLIMBING_ALGORITHM.md](HILLCLIMBING_ALGORITHM.md) and [ITERATION_RUNBOOK.md](ITERATION_RUNBOOK.md).
**The metric:** the in-band rate (1–3/5 passes for terminus-2 + glm-5.3-flash), per family and
in total. **Done when:** at least 80% of a batch of 10+ is accepted in band.

**Notation:**
- a pass pattern lists the 5 rollouts (✓ pass, ✗ fail, · not run or invalid);
- `v1 5/5 → v2 2/5` is a revision's pass counts.

**Costs** are the pipeline's ledger. It prices every prompt token at the uncached rate
(`authoring_harness.py`), so on long authoring sessions it reads **2–3× the gateway's own
`gateway_cost_usd`** (t10: $6.47 on the books vs $2.18 at the gateway).

**The team tonight:**
- **manager (task-builder):** decisions, prompt and revision-logic edits, commits to main,
  deploys, batches, this log;
- **executor-worker:** solver trajectories and the Trace Parser;
- **api-service:** authoring stages and revisions;
- **infrastructure:** the cluster.

## Iteration 0: aws-agent-b1

- **Code:** `d043fc3`:
  - infra-authoring: tune-r3 prompts, soft time limits, the docker wrapper, leak timeouts.
- **Run:** submitted 09:49 UTC; 10 tasks; dial 2; budget_usd 120.
- **Ended:** 13:16 UTC (3 h 27 min; the last v2s settled past the ~3 h cap); spend $53.13 total on the books.
- **Spend:** about $50 on the books for the batch (the pipeline total was $23.36 at 10:41 and
  $48.61 at 12:13).

### Result

- **In band: 0 of 10.** Every first version that reached the solver was too easy (4–5/5).
- **Lost:** 3 rejected (leak review), 2 cancelled.
- **Revisions:** the path works end to end; see "Revisions" below.

| Task | Family / mechanism / env | Passes per version | Stage reached | Cost | Finding | Change (iteration 0 → 1) |
|---|---|---|---|---|---|---|
| t01 | performance / exact_semantics_under_speedup / Go | v1 5/5 → v2 not evaluated | CANCELLED at the cap (v2 in create) | $6.21 | The textbook bottleneck (rescans and re-sorts 100k events per report), found at step 3. The semantic edge (empty account = wildcard) was caught by the agents' own old-vs-new differential checks. Its create burned 24 of 51 steps on empty 10 s polls. | Polling: wait with `duration` 60 while a check runs, never 10 s polls (task_author.md + world.md "Before you finish"). Expected: about 5× fewer polling steps, about 40% off a long create. |
| t02 | compilers / stateful_incremental_parse / Python | v1 5/5 → v2 never evaluated | REJECTED at v2 (leak review ×3) | $4.30 | v1: a one-file stale-cache fix, cued by "schedules containing redefinitions"; it reached rollouts only through the placeholder fail-open. v2's mutation got `add_persistent_state` from a keyword misfire (bare "cache"). Its world was a real cross-component defeat, but the honest invariant ("across process boundaries … same as a newly constructed evaluator") implies the persistence, so all 3 drafts failed the blind check. | `process_local_guard` matches only unambiguous evidence (dropped "cache", "set of", "dict of", "flag"; the code pattern `global ` → a Python `global` statement). Expected: mutation operators that fit the passing fix. |
| t03 | algorithms / optimality_required / C | v1 ✗✗··✗ (3 false fails) | CANCELLED (stopping rule: >1.5 h, a diagnosed task defect) | $2.49 | A task defect: the test generator can make a job its own prerequisite (`range(rng.randint(0, min(2, i)+1))` includes `j == i`), though the instruction requires an acyclic graph. The reference accepted it, so solvers that enforce the rule failed. Both runs that were read fixed the real planning bug. Two later runs timed out at 3600 s. | task_author.md Tests: random inputs obey every rule the instruction states, asserted in the generator. Expected: no false fails on generated inputs. |
| t04 | distributed / lost_acknowledged_write / Python | v1 5/5 → v2 5/5 | v2 OUT_OF_BAND (too easy) at the batch end | $4.2+ | v1: an omission (commit() never called; recovery truncates prepared entries), found at step 3 on one read. v2's mutation (a credit limit that the v1 fix now violates) is a genuine interacting-requirements defeat. Its first draft failed the blind check on the PARENT's fix (commit before ack), which the judge compared with the whole fix. Its world died twice from the agent's own `exit $rc` / `set -eu` at the prompt (fixed: fix-terminal-exit). | The leak judge of a mutated world knows the parent's fix is already solved (lineage `defeats` in the judge's context). Expected: mutations stop failing the blind check on their inherited part. |
| t05 | database / crash_between_commit_and_effect / SQLite | none | REJECTED v1 (leak review ×3) | $3.54 | A harsh judge: "exactly one ledger record per charge after any crash" → the textbook idempotent append + flock + fsync, judged "specific" while the judge waived a wrong file. Also, the world exposed a stdin pause as its kill point (a fault hook), which the instruction had to describe. | The blind judge: a right mechanism in the wrong place is generic; textbook remedies for the stated guarantee are generic; a prediction containing the wrong fix isn't specific. (The fault-hook lesson is queued for iteration 1's world.md Rules.) |
| t06 | cross-component / multi_step_causal_failure / Go | v1 5/5 → v2 not evaluated | CANCELLED at the cap (v2 world building) | $5.22 | Its first draft leaked through wording: guarantees 2–3 restated the counterexample's two sequences as conditionals. v1: the most effort of any task (37–83 steps, 35–56 min each), but the root cause was a visible key mismatch (`api:<order>` vs `charge:<delivery>`), found at step 3. One rollout was dead-lettered by KEDA scale-down kills (fixed: release-refund). | task_author.md Instruction: guarantees hold over all histories, and the symptom isn't the triggering inputs; never restate the counterexample as a symptom or a conditional guarantee. |
| t07 | distributed / adversarial_schedule / Python | v1 5/5 → v2 not evaluated | CANCELLED at the cap (v2 in create) | $3.89 | The hardest v1 by effort (median 37 steps, 30 min; one run timed out at 3600 s while reviewing), but every lost-update path was visible in service.py, so diligence won. | None: the lesson is t04's ("visible on one read"), queued for iteration 1. |
| t08 | algorithms / greedy_counterexample / C | v1 5/5 → v2 not evaluated | CANCELLED at v2 (stopping rule: the create wedged on an oversized heredoc, 24+ min repeating C-c/EOF) | $4.16 | v1: the world SHIPPED the exact solver (`--reference`), so every run routed the greedy mode through it. The v2 world removed it and made v1's strategy its wrong fix, despite a misfired `add_persistent_state` operator. | world.md Rules: the correct behavior is not already in the system (no second mode, reference path or recompute-from-scratch routine in /app). Expected: algorithm worlds where the solver must design the exact algorithm. |
| t09 | performance / memory_bound / Java | v1 5/5 → **v2 4/5** (✗✓✓✓✓) | CANCELLED at the batch end (v3 would run on the old code) | $5.49 | v1: "6M events / 128 MiB" dictated "stream it" (3–8 min each). The v2 mutation (checksums over an account's events in timestamp order, which isn't the stream order) came from the analysis's own suggestion. It passed a GENUINE leak review, and the blind predictor walked into the trap. v2's first failure is a real model failure: the right external-merge design, broken in implementation. | Performance brief: the obvious optimization silently changes an exact result; the hard part is keeping the semantics. |
| t10 | database / isolation_anomaly / PostgreSQL | none | REJECTED v1 (leak review ×3) | $8.27 | A textbook lost update (oversell on one row): any honest invariant lets a reviewer predict the standard fix (a conditional UPDATE / FOR UPDATE), and the family brief itself suggested "oversold stock". The create took 89 steps, 49 min, 40 of them empty polls. | Database brief: never a plain lost update / read-check-write on one row; the DBA's first fix is the plausible wrong fix; the "oversold stock" example replaced. |

### In-band rate

| Family | Tasks | In band | Notes |
|---|---|---|---|
| performance_resources | t01, t09 | 0 | v1s 5/5; t09 v2 4/5 (toward the band) |
| compilers_parsers | t02 | 0 | v2 rejected (leak review) |
| algorithmic_constraints | t03, t08 | 0 | t03 a test defect; t08 shipped its exact solver |
| distributed_concurrency | t04, t07 | 0 | v1s 5/5; t04 v2 5/5 (an additive mutation) |
| database_consistency | t05, t10 | 0 | both rejected by the leak review |
| cross_component_stateful | t06 | 0 | v1 5/5 |
| **Total** | 10 | **0 (0%)** | |

### Revisions (the revision pipeline)

- **The path works end to end on the cluster.** t02's v1 world was authored on node
  ip-10-42-0-140, and its mutation ran on ip-10-42-1-42, downloaded the parent from S3, and
  built and checked a new world. Then: create → repair1 → repair2, with every trace in S3.
- **Per revision:**

| Task | Revision | Operators | Source | Result |
|---|---|---|---|---|
| t02 | v1 5/5 → v2 mutation | remove_cues, add_persistent_state | keyword misfire ("cache") | world OK (a real defeat); FAILED outright (leak ×3) |
| t09 | v1 5/5 → v2 mutation | remove_cues, introduce_global_invariant | the analysis's suggestion | world OK in 12 min; genuine leak pass; **v2 4/5: toward the band** (the only revision that moved) |
| t08 | v1 5/5 → v2 mutation | remove_cues, add_persistent_state | keyword misfire ("set of"/"global ") | world OK (reference mode removed); create wedged, cancelled |
| t04 | v1 5/5 → v2 mutation | introduce_global_invariant, generate_counterexample | defaults | world OK in 11 min; leak repair1 genuine; **v2 5/5: no movement** (an additive mutation kept v1's defect) |
| t01 | v1 5/5 → v2 mutation | remove_cues, introduce_global_invariant | self_checking_agent (a good fit) | world OK; cancelled in create at the cap |
| t07 | v1 5/5 → v2 mutation | remove_cues, introduce_global_invariant | self_checking_agent | world OK; cancelled in create at the cap |
| t06 | v1 5/5 → v2 mutation | remove_cues, add_persistent_state | keyword misfire (the world was already persistent) | cancelled while building at the cap |

- **Operator tally:**
  - `add_persistent_state` 3× (t02, t08, t06): all misfires, fixed on main by narrowing
    `process_local_guard`;
  - `introduce_global_invariant` 3× (t01 good; t09, t07 partial);
  - the defaults 1× (t04, partial).

- **t09 v2's rollouts:** the mutation worked. v1's plain stream fails for a real reason: the
  checksum needs timestamp-then-sequence order under the same 128 MiB heap, so an external
  sort/merge is needed.
  - PASS r3 (30 steps, 20 min): saw at step 4 that "one streaming pass would fix the OOM but not
    timestamp-ordered checksums", built a disk-backed external sort and checked it against the
    6M-event oracle under -Xmx128m.
  - FAIL r1 (10 steps, 15 min): the right design, shipped broken (a BufferedOutputStream never
    flushed). It found its own bug at step 10, then ended without verifying: a genuine model
    failure.
- **t04 v2 FINAL 5/5 (v1 5/5 → v2 5/5: no movement), 5–35 min per run (~4× v1's effort).**
  - **The cause:** the mutation was ADDITIVE. It added a credit-limit invariant but kept v1's
    defect in place ("Cluster.append … never calls commit", r1 and r3 at step 3), so v1's
    strategy plus one validation check passes. That breaks world_mutation.md's own rule that
    the old strategy must fail.
  - **Also seen:** one solver step degenerated into garbage text (a glm-5.3-flash glitch at
    reasoning high); the run recovered and passed.
- **Cancelled at the cap (12:45):** t01, t06, t07's v2s (still in authoring, ~1 h more each).
  t01's and t07's v2 worlds passed world check; t06's was still building.
- **Metrics per family:** revisions started 8 (t02, t09, t04, t08, t01, t07, t06; t04 v3 cancelled at once); every mutated world that finished passed world_check on the first try; moved toward the band 1 (performance, t09: 5 → 4); no movement 1 (distributed, t04: 5 → 5); landed 0; overshoots 0;
  failed outright: 1 (compilers_parsers, t02).
- **Findings:**
  - **The mutation author aims at the analysis's `target_fix` even when the operator
    misfires** (t02, t08 both produced real defeats). So the analysis matters more than the
    operator list.
  - **`remove_cues` + `introduce_global_invariant` is now the near-default pair:**
    `self_checking_agent` fires on almost every run, since every solver fuzzes or diffs its
    own fix.
  - **A mutation that adds hidden state must keep it out of the invariant and the symptom**
    (t02 v2).
- **Leak-review exhaustion was the batch's biggest loss** (t05, t10, t02 v2): structural leaks
  can't be reworded away. Ready for iteration 1: branch `r5-leak-mutation` routes 2
  consecutive blind-"specific" drafts to a world mutation instead of rejecting.

### Bugs and infrastructure fixed during iteration 0

All are on main; they deploy before iteration 1.

- **Leak review fail-open:** blind predictions of `"..."` (`61d0a4d`) or `"See detailed answer
  below."` (`e1a1451`) were judged "none" and passed the gate (t02, t07, t08 and t01's v1s
  reached rollouts that way). Now they're retried, then recorded as inconclusive.
- **Agents ending their own terminal** (`exit $rc`, `set -e` at the prompt kills the tmux
  session): fix-terminal-exit guards the authoring image's interactive shell, plus a prompt
  line. Solver rollouts keep INVALID + retry (2 of 42 rollouts).
- **KEDA scale-down killed busy executor pods,** and each kill used one of 3 attempts (a
  rollout was dead-lettered): release-refund (a deliberate release refunds the attempt; scale-down
  window 120 s → 900 s).
- **Observability:** tb3d-analyze-trace shows each version's analysis (operators, rules fired
  with their evidence, pass review, signals) in `GET /v1/tasks/<id>`.
- **Scheduling:** spread-workers (a soft topology spread for generation and executor pods).
- **No total budget cap,** by the user's decision at 11:45 UTC: `SDE_BUDGET_USD=none`
  (`4da4386`). SSM `/sde/budget_usd` is set directly, not through Terraform. That's drift: a
  later `terraform apply` resets it to 220 unless var.budget_usd changes too.

### Solver INVALID causes (42 rollouts by 11:58)

| Cause | Count |
|---|---|
| VALID_PASS | 30 |
| VALID_FAIL | 3 |
| INVALID: the solver closed its own terminal | 2 |
| INVALID: the 3600 s solver timeout | 3 |
| INVALID: infrastructure | 0 |
| DEAD (1 dead-letter, 3 cancelled with t03) | 4 |

### Queued for iteration 1 and later (not changed this batch)

- **world.md Rules** (used by change (a) this batch):
  - never a missing call or step (t04, t07);
  - no stdin pause or prompt as a kill point (t05).
- **Performance brief, sharpened:** the changed result must escape an old-vs-new random diff
  (t01).
- **world_mutation.md:**
  - durable state a mutation adds must not be implied by the invariant or the symptom (t02 v2);
  - a mutation adds reasoning, not work: t06's runs took up to 56 of the solver's 60 minutes,
    and a timeout is INVALID, not a fail;
  - the defect v1's agents found must itself change (move, become conditional, need a
    different fix), not stay in place with a new requirement added next to it (t04 v2: 5/5
    again).
  These are all in one section ("What makes a good mutation"): one per batch, the strongest
  first. The strongest (api-service, from t04 v2 vs t09 v2): **"the new difficulty must be
  something a careful rewrite gets wrong, not only a minimal patch; a stated invariant a
  from-scratch implementation naturally satisfies adds nothing"**. t04's solvers rewrote
  append/recovery and checked the stated credit limit as a matter of course. t09's new invariant
  (an ordered checksum within 128 MiB) stays hard to implement even when understood.
- **Cancel accounting:** a cancelled authoring trial's cost is booked when it settles (t06
  $4.55 → $5.22, t07 $3.28 → $3.89 after their cancels), about $0.6 per cancelled create.
- **task_author.md Environment:** keep each command under about 4 KB (t08 v2's wedged
  terminal).
- **Revision logic:**
  - r5-leak-mutation (leak exhaustion → a mutation);
  - rank `self_checking_agent` below the specific rules;
  - prefer the analysis's suggestions.
- **The blind predictor:** a max_tokens cap (24k-character essays hit the 180 s timeout).
- **Difficulty:** if iteration 1 is still mostly 4–5/5 with these changes, raise the dial to 3.

## Iteration 1: aws-iter1 + aws-iter1-alg

- **Code:** `9db8b68` (main): every iteration-0 change and bug fix above.
- **Deployed:** 13:18 UTC; infrastructure verified each change live.
- **Budget:** SSM `/sde/budget_usd` = `none`; /healthz `cap_usd: null`.
- **Submitted:** 13:19 UTC; dial 2; no budget_usd.
- **Composition:** aws-iter1's sampler gave compilers_parsers twice (t02, t05) and no
  algorithmic_constraints, so t05 was cancelled at once and replaced by aws-iter1-alg
  (1 task, area algorithmic_constraints). Iteration 1 is 6 tasks, one per family, with no
  mechanism repeated from iteration 0.
- **Expected from the changes:**
  - fewer leak rejections (the fairer judge, the mutation context, the placeholder retry);
  - harder v1s (no correct path in /app; the perf and database briefs);
  - fewer polling steps;
  - mutation operators that fit the passing fix.

| Task | Family / mechanism / env |
|---|---|
| aws-iter1-t01 | cross_component_stateful / misleading_symptom / Go |
| aws-iter1-t02 | compilers_parsers / aliasing_side_effects / Go |
| aws-iter1-t03 | performance_resources / scalability_wall / Java |
| aws-iter1-t04 | database_consistency / write_skew / SQLite |
| aws-iter1-t06 | distributed_concurrency / linearizability_violation / Python |
| aws-iter1-alg-t01 | algorithmic_constraints / global_constraint_interaction / C |

### Result (ended 16:20 UTC, the ~3 h cap; spend on the books $53.5 → ~$92)

- **In band: 0 of 6.** Closer than iteration 0: t01 v1 4/5 and t06 v2 4/5; every scored v1
  was 4–5/5 (iteration 0: 5/5).
- **Lost:** t03 rejected (leak review ×3); t04 cancelled (its create wedged in an oversized
  heredoc).

| Task | Passes per version | End | Finding | Change (iteration 1 → 2) |
|---|---|---|---|---|
| t01 (Go ledger, SQLite) | v1 4/5 → v2 (repair_ambiguity) 5/5 settled | cancelled at the cap | Run 3's ✗ was a genuine miss of crash-time PENDING recovery, but its tests insert raw SQL into a `transactions` table the instruction never names (a hidden requirement). The repair missed it because the failure explanation was cut at 303 chars: an unescaped `"` ended the strict-schema string. | task_author.md Tests: never write the system's DB tables or state files directly. Plus: the failure review's `conclusion` comes first, uses single quotes, and the cut is flagged (r6-explanation-uncapped). |
| t02 (Go interpreter) | v1 5/5 → v2 not evaluated | cancelled at the cap | The instruction was leak-clean, but the CODE gave it away: `findTopLevelAssignments`, 4 small files read in full by step 2, and a `show` command that prints the bad rewrite. The v2 mutation's operator misfired ("reorder"), and the cue moved to `collectDirectWrites`. | families.py compilers: 2+ components could plausibly produce the symptom; no command prints the transformed form. Plus `limit_named` (below). |
| t03 (Java perf) | none | REJECTED (blind leak ×3) | The perf trap (a HashMap loses the required order) is guessable once the instruction states the order: `LinkedHashMap` every time. | families.py perf: the exact result is an outcome no standard structure gives for free. |
| t04 (Py staffing, SQLite) | none | cancelled (stopping rule) | The world took 61 min (32 of it stalled provider calls). Its create then wedged in an oversized heredoc for 15+ min ($4.59, the most expensive job). | task_author.md Environment: keep each command under ~4 KB. |
| t06 (Py KV store, WAL) | v1 5/5 → **v2 4/5** | cancelled at the cap (v3 starting) | v1: recovery.py's docstring said "Only entries that have been committed": the bug. v2's mutation (another misfire, "in memory") still made a real two-path defect, and remove_cues cleaned the docstrings. Its one ✗ broke standalone startup, not the hard part. | world.md: names, comments and docstrings on the defective path never state the limit; `sde-world check` warns `limit_named` (tb3d-limit-names). |
| alg-t01 (C allocator) | v1 5/5 → v2 building | cancelled at the cap | The instances are small, so the solver writes its own exhaustive search; the world rule held (brute force only in repro/). Its create was 54 min / $7: 39 min of model writing and debugging. | families.py algorithms: stated sizes where exhaustive search can't finish in the time limit. |

**In-band per family:** 0 everywhere (performance 0/2, compilers 0/1, algorithms 0/1,
database 0/1, distributed 0/1).

**Revisions:**
- t06 5 → 4 (toward the band, no overshoot);
- t01 4 → 5 (repair_ambiguity; the truncated explanation made it fix the wrong thing);
- t02 and alg-t01 cancelled before evaluation.
- Keyword misfires: 2 of 3 mutations ("in memory", "reorder"). Every finished mutated world
  passed world_check on the first try.

**Measurements (api-service, executor-worker, and the manager's trace timelines):**
- **Leak, 12 reviews:** blind specific 5 (iteration 0: 8 of 9), generic 6, none 1; 0
  placeholders. The "failing sequence" escape was cited 3× (fixed on main 9895089). Repairs
  converge when they DELETE sentences (t02 r1, t06 r2).
- **Polling:** 10 s polls gone (60 s waits); empty steps 28% (36%), 29% of prompt tokens (46%).
- **World time:** 32–64 min. Trace timelines: t04's 61 min = 32.5 min of stalled model calls
  (one took 1214 s for 310 tokens) + 27 min of work; t02's 49 min = 10.3 min stalled + 32 min.
  SDE_AUTHORING_LLM_TIMEOUT_S was unset, so litellm's 6000 s default applied.
- **Create time:** t01's 36 min = 21 min waiting on 3 `sde-task run`s (4 roles run one after
  another; #1 hung 12 min on its own test's bug). alg-t01's 54 min = 39 min model work + 7
  runs of 1–2 min.
- **Infrastructure:** release-refund's 900 s scale-down window works (2 scale-downs, 0
  re-claims, 0 new dead-letters). The analysis view returned HTTP 500 for decided versions with
  an even number of passing runs (fixed: tb3d-analysis-files-fix).

### Shipping in iteration 2 (main, deployed before aws-iter2)

- **Difficulty:**
  - dial 3;
  - world authoring at reasoning **high** (task authoring stays medium; the user's call);
  - the per-task changes above;
  - the distributed brief (3+ node processes, a defect needing 2+ interleaved events, the loss
    away from its cause).
- **Mutations:**
  - 2 difficulty operators per mutation (remove_cues no longer takes a slot);
  - the trusted review's first suggestion outranks keyword rules (r6-calib-reorder);
  - files_modified counts real files only (r6-files-modified);
  - the mutation author gets the passing runs' condensed traces (r7-solver-runs) and the parent
    world's hardness results (was always "(not recorded)");
  - world_mutation.md: the new difficulty must defeat a careful rewrite.
- **Speed:**
  - authoring call timeout 600 s (worlds 1200 s);
  - `sde-task run` runs the roles in parallel, 2 at a time (−28% measured), with progress lines;
  - iterate with `--as oracle` first;
  - validate and world_check adopt the author's stamped checks instead of re-running Docker
    (tb3d-adopt-author-checks).
- **Leak:**
  - the judge's location rule without the escape;
  - "remove sentences, never add" in repair feedback;
  - pointer placeholders retried.
- **Not yet (the user's call):** the world-time leak check, the repro/explore.py schedule
  explorer, merging world and task authoring (kept separate).

## Window 1 (sliding window, six lanes): started 2026-11-18 ~21:00 UTC

**Release:** 5e1073985063 (main 4916619 + join-resume), deployed ~20:55-21:00 UTC with the bundle:
drain on deploy, per-release image tags, DB-blip ride-out, Spot interruption handler, dashboard
(leak-review panel, Prod / Dev). Cluster: 17 nodes (9 on-demand, 8 Spot), RDS db.t4g.medium,
generation 11 x 3 sessions, executor KEDA 2-10 x 3 agent runs, validate 4.

**Changes in this window** (one per lever):

| Lever | Change | Evidence | Expected |
|---|---|---|---|
| task_author.md (instruction) | the symptom never states the triggering inputs, sequence or state; a mutated world's create never restates the case the mutation added | leak-review failures: aws-iter2-t03 v1 ("a retry reserves units again"), t05 v2, t11 v2, t02 v2, iter1-t01 v3, iter1-t06 v3, b1-t03 v3 | fewer semantic / blind-specific leaks, especially on mutated worlds |
| world.md (rules) | "not the textbook missing piece": when the honest guarantee implies a standard remedy, the remedy is already in the system, subtly wrong | aws-iter2-t12 (leak x3: idempotency key with the effect), t02 v2 (scope chain), the perf family (one index) | fewer worlds whose guarantee names the fix |
| calibration.py | keyword rules read only commands that write the task's code (not the solvers' helper scripts) | aws-iter2-t04 v3 got inject_partial_failure from `= set()` in a Python benchmark of a Java task | fewer misfired mutation operators |

**Tasks:** 25, all window 1 (jobs claimed by the old pods were handed back and re-ran on the new
release).

| Lane | Session | Batch | Families | Lever |
|---|---|---|---|---|
| L1 | api-service-15 | aws-w1-l1 | cross_component_stateful, database_consistency | world.md |
| L2 | ...-ba | aws-w1-l2 | compilers_parsers, algorithmic_constraints | family briefs |
| L3 | ...-29 | aws-w1-l3 | performance_resources, distributed_concurrency | task_author.md |
| L4 | ...-9b | aws-w1-l4 | all six (drew algorithmic + compilers only) | world_mutation.md, calibration.py |
| L5 | infrastructure-be | aws-w1-l5 | all six | harness, self-checks, classify; infrastructure |
| L6 | (no session yet) | | | spec sampler and dial |

Lanes top up one task per decided task. Queued for window 2: calibration `add_persistent_state`
as a last resort (L4, 9af8ab7: 0 of 7 such mutations reached the band).
