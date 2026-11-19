# How we optimized task creation

As of 2026-11-19 00:10 UTC. Production K3s cluster (AWS), pinned solver terminus-2 + glm-5.3-flash.

**Goal:** novel, diverse tasks that the solver passes 1-3 times out of 5 ("in band"), accepted only
when the failures are the model's, not the task's.

**Headline:** we went from **0 in-band tasks in the first 16** (iterations 0-1, every first version
4-5/5) to **about half of decided tasks accepted in band** (iteration 2 at 42%; the sliding-window
lanes at 10 accepted of ~19 decided), while the **cost per accepted task fell from ~$40 to ~$4-7**.

Numbers marked *measured* come from the pipeline's ledger and docs/ITERATION_LOG.md. Numbers marked
*estimate* are projections for changes deployed tonight that haven't been scored yet; each says what
it's based on.

---

## 1. The loop: every agent run is feedback for the next version

```
generate world → world check → author task (+ leak review) → validate
      → 5 agent runs → Trace Parser analysis → decide
            1-3 pass  → ACCEPT (only if the failures are model-caused)
            4-5 pass  → too easy: mutate the world (next version)
            0 pass    → too hard: ease the instruction (next version)
            ambiguous → repair the instruction
      up to 8 versions per task
```

- **Each version is judged on 5 agent runs.** The Trace Parser reads every trajectory and rules each
  failure a genuine model failure or a task defect (INVALID, retried). Its findings feed the next
  version and the next prompt change.
- **Sliding window, six lanes.** Instead of submit-a-batch-and-wait, six agent sessions each run
  ~1/6 of the cluster with their own strategy and their own lever (prompt section, family brief,
  mutations, calibration, selection, infrastructure). A manager merges one change per lever per
  window and deploys it with drains, so no running work is lost.
- **Each change is judged on the tasks authored after it** (every job records its release sha).

## 2. Four levers we found

### Lever 1: prompt engineering (task quality is very sensitive to how the author is instructed)

| Finding (evidence) | Rule added | Effect |
|---|---|---|
| The instruction named the fix: "keep the order" → `LinkedHashMap` every time (aws-iter1-t03, rejected for leaks ×3) | State the requirement as an **outcome** ("the same output as now"), never the structure, tie-break or edge case to keep | Perf instructions passed the leak review after 2 repairs instead of failing 3 (measured, aws-w3-l3-t01) |
| The code gave the bug away: `findTopLevelAssignments`, a docstring saying "only committed entries" | Names, comments and docstrings on the defective path never state the limit; `sde-world check` warns `limit_named` | v1 pass counts dropped from 5/5 to 4/5 in the next iteration (measured) |
| Worlds shipped the correct algorithm (`--reference`, `plan_reference()`); one fix was `return plan_reference(...)` | Never ship a correct, reference or fallback version; the brute-force reference lives only in the hidden tests | Removes one-line swaps (estimate: the most common too-easy pattern in algorithms) |
| Tests wrote the system's DB tables directly (a hidden requirement) | Tests use only the interfaces the instruction names | Fewer task-blamed failures (measured: the repaired versions scored cleanly) |
| A correct fix failed on rows the agent's own testing left behind (aws-w2-l1-t01) | Each test starts from clean state it creates | Fewer INVALID runs at 1-3/5 |
| A test harness deadlocked on an unbounded read (aws-w2-l1-t02 verifier timeouts) | Every blocking read has a timeout; run processes one at a time unless the overlap is stated | A hang fails one test instead of the whole verifier |
| A 35 KB heredoc wedged an author's terminal for 60+ minutes | Commands under ~4 KB, big files in parts, no nested heredocs, plus a terminal self-heal | No more stuck authoring sessions (deployed 23:45) |

### Lever 2: taxonomy seeding (choose the bug family, mechanism, system, language and difficulty)

The single biggest diversity change, deployed 2026-11-18 23:50:

| Dimension | Before | After |
|---|---|---|
| Application domains | none (the author picked, mostly ledgers: ~25 of 104 worlds) | **40 kinds of systems + 10 language systems**, least-used first; no ledgers for performance |
| Mechanisms (bug patterns) | 22 | **62**, each with a one-line defect note given to the author |
| Performance mechanisms | 3 | **16** (weight 1.5×) |
| Algorithmic | 4 (2 leak-prone) | **12** (greedy-vs-optimal and "be optimal" retired) |
| Distributed | 4 | 11 |
| Database / compilers / cross-component | 4 / 4 / 4 | 8 / 7 / 8 |
| Languages | drawn against all history (5 of 5 new distributed worlds drew Go) | balanced within each family |

Why the new mechanisms are harder, by family:
- **Performance:** the obvious speedup (index, cache, batch, stream, parallelize) silently breaks a
  semantic the slow path gets for free: an order, a tie, a late reference, an acked write's
  durability, an exact total. Example: `spill_preserves_first_seen`, `reassociation_drift`,
  `backpressure_reorder_window`.
- **Distributed:** every textbook piece is present and correct on the happy path; the bug is how two
  of them interact on a failure path. Every distributed 5/5 tonight was a *missing* piece read off the
  code at step 3; the one in band had every piece present.
- **Algorithmic:** the defect hides from random testing (it needs a specific structure or edit
  history). The solver's winning move was fuzzing small inputs against brute force: the retired
  mechanisms went **0 of 5** and **0 of 6** accepted (measured).

*Estimates for the expanded taxonomy:*
- Distinct (domain × mechanism) combinations: **~20× more** (≈90 before, since the domain was in
  effect fixed, → ≈2,000+ now).
- Near-duplicate tasks (same system, same bug class): **from ~1 in 4 to under 1 in 20**.
- Leak rejections: **down ~40-50%**, from retiring the two mechanisms behind most algorithmic leaks
  and stating requirements as outcomes.

### Lever 3: the revision pipeline

| Change | Measured effect |
|---|---|
| **Mutation operators** (remove cues, add concurrency or partial failure, a global invariant, a cross-component dependency, a counterexample) | Mutations land tasks in band: 5 of tonight's accepts came at v2-v4 (e.g. 5/5 → 4/5 → 0/5 → 3/5) |
| **Cross-component first for performance** | Perf mutations with it: **2 of 2** reached the band; without it: **0 of 5** |
| **Persistent-state operator demoted to last resort** | 0 of 7 such mutations had reached the band |
| **Early stopping**: once the verdict is certain, skip the remaining agent runs | Saves up to 2 of 5 runs per decided version (~20-40% of agent-run time on those tasks; estimate) |
| **Timeout review**: a timed-out run goes to the Trace Parser, which rules it a model failure or INVALID | Timeouts no longer silently count as passes or fails |
| **Variants** (reuse a world, plant a new mechanism's bug, fix the old one) | Replaced rewordings of the same task; now superseded by new worlds on the new taxonomy |

### Lever 4: scope, response length and semantic time limits

- **Scoped authoring:** short, user-voiced instructions; comprehensive hidden tests; 2-file fixes
  turned into 3+ module worlds for performance (too-easy perf worlds were "3 small files, read in
  one step").
- **Prompt budgets enforced by tests:** task author ≤ ~3,650 tokens, world prompt ≤ ~2,800, measured
  over every family × mechanism × shape production renders.
- **Semantic time limits** in the prompts (stop once the world check passes; warnings aren't gates)
  keep authoring focused, separate from the hard 1-hour solver timeout, which we never shorten.

## 3. Throughput

| | Start of night | Now |
|---|---|---|
| Authoring sessions | 33 (11 pods × 3) | **42** (14 × 3) |
| Agent-run slots | 2-10 executor pods × 3 | **up to 16 pods × 2**, ~59 concurrent runs at peak |
| Deploys | lost in-flight sessions | **drained**: generation and executors finish running work first |
| Stuck sessions | heredoc wedges (60+ min each) | self-heal + 4 KB rule |
| Spend | ~$35-40/h | ~$110/h (more capacity, no budget cap) |

## 4. Results

| Window | Tasks decided | Accepted in band | Rate | Cost per accept | Notes |
|---|---|---|---|---|---|
| Iteration 0 | 10 | 0 | 0% | n/a | every v1 too easy (5/5) (measured) |
| Iteration 1 | 6 | 0 | 0% | n/a | v1s 4-5/5, closer (measured) |
| Iteration 2 | ~12 | 5 | **42%** | ~$15 | first accepts, mostly via mutation (measured) |
| Windows 1-2 (six lanes) | ~19 | 10 | **~50%** | **~$4-7** | 5 at v1, 5 via mutation; every rejection a leak, none a gate or infra failure (measured) |
| New taxonomy (from 23:55) | 0 so far | — | **55-65% (estimate)** | **~$5 (estimate)** | 32 new worlds building; first verdicts ~01:00-01:30 UTC |

First new-taxonomy performance verdicts (L4, 00:59-01:35 UTC; measured): the 3 scored v1s were all
too easy (lock_convoy 5/5, lsm_compaction 4/4 and coarse_invalidation 4/4, both early-stopped), and
group_commit_durability was rejected for a leak. The too-easy ones escaped the planted trap by deferring
the work (compaction only on demand, a lazy rebuild on read, the fetch moved out of the lock), which the
workloads didn't time. Two fixes went to the manager: the perf brief's bound now covers the reads
(lane-l4-perf-brief-escape), and the group-commit note describes a durable-but-slow baseline
(lane-l1-group-commit-note).

Basis for the estimate: leaks caused every windows 1-2 rejection; if retiring the leak-prone
mechanisms and outcome-only requirements remove 40-50% of them, the in-band rate rises from ~50% to
the 55-65% range. The 80% goal is not met yet.

## 5. What's next

- Score the new-taxonomy window (32 new worlds) and judge each brief change on it.
- The instruction still names the textbook problem in some algorithmic tasks ("total weight as high
  as possible" → weighted interval scheduling); that's L3's next task_author rule.
- Watch CPU with 42 authoring sessions; nodes burst to ~100% during world builds.
