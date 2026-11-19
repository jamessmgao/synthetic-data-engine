# Validation phases: every gate that can stop a task

As of 2026-11-19, from the code on main and the records of all 150 tasks on the production
cluster. Related: [VALIDATION_GATES.md](VALIDATION_GATES.md) (the gates' design) and
[VALIDATION_AND_FAILURE_MODES.md](VALIDATION_AND_FAILURE_MODES.md) (six accepted tasks and
why agents fail them).

## Overview

A task must pass up to 15 checks across four phases before it is accepted. Each check can
end the task with one of three rejection reasons:
- `invalid`: the task or world is broken;
- `exhausted`: still out of band after 8 versions;
- `infra_unresolved`: a run slot never got a valid measurement.

Most gates feed back into a repair first, at most 2 repairs per version, so a rejection
means the pipeline already tried to fix it.

| Phase | Check | What it catches | On failure |
| --- | --- | --- | --- |
| World | structure | missing `repro.sh`, `fix.sh`, `world.json`; no invariants or counterexample; a base image off the allowlist | world repair, then `invalid` |
| World | build | the world's image doesn't build | world repair, then `invalid` |
| World | repro_fails (×2) | a bug that isn't real or isn't stable: repro must exit 1 twice | world repair, then `invalid` |
| World | repro_passes_with_fix (×2) | a bug the world's own fix doesn't fix | world repair, then `invalid` |
| World | hardness (warn mode) | a symptom an agent can't see, a local one-file fix, a wrong fix that isn't tempting, a function named after its own limit | recorded; fails only in `enforce` mode |
| Task | structural | a bundle that breaks the Harbor task layout or `task.toml` schema | repair, then `invalid` |
| Task | lint | `harbor lint` findings that must be fixed | repair, then `invalid` |
| Task | oracle (×2) | a reference solution that fails its own tests, or passes only sometimes | repair, then `invalid` |
| Task | nop | tests that pass on an untouched container | repair, then `invalid` |
| Task | wrong_fix | tests a plausible wrong fix still passes | repair, then `invalid` |
| Task | leak: semantic review | an instruction that states the fix or the counterexample, directly or in other words | repair, then `invalid` |
| Task | leak: blind prediction + judge | an instruction from which a reviewer who sees nothing else can predict the real fix and where it goes | repair, then `invalid` |
| Evaluation | classify | a run that didn't measure the model: harness exceptions, wrong agent config, a non-binary reward | `INVALID`, retried up to 3 times, then `infra_unresolved` |
| Evaluation | timeout review | a run at the 1-hour limit: was it the model's fault? | model → `VALID_FAIL`; otherwise `INVALID` and retried |
| Evaluation | failure review | a failure the Trace Parser blames on the task, not the model | `repair_ambiguity` revision, even when the pass count is in band |

A task that passes every gate and lands at 1-3 of 5 passes, with no failure blamed on the
task, is accepted. Anything else out of band is revised: a world mutation when too easy, an
easier version when too hard.

```
 World check ──► Task gates ──► Leak review ──► 5 agent runs
 structure,      structural,    semantic        classify each run
 build,          lint,          review,         timeout review
 repro ×2,       oracle ×2,     blind           failure review
 fix ×2,         nop,           prediction,          │
 hardness (warn) wrong_fix      judge                │  INVALID: retry up to 3,
      │              │              │                 │  then infra_unresolved
      └──────────────┴──────────────┘                 │
                     ▼                                 ▼
   a failing gate: repair, at most 2 per version     decision
   still failing after that: rejected invalid        ├─ 1-3 of 5, no task blame ─► ACCEPTED
                                                      ├─ a failure blamed on the task ─► repair the task
                                                      └─ 4-5 mutate, 0 ease; version 8 ─► exhausted
```

Every gate before the agent runs exits to a repair; the runs exit to a retry; the decision
exits to a revision.

## World phase: is the bug real, stable and fixable?

No world has failed `world_check` in production across 150 tasks. The world author must run
the same check (`sde-world check`) before it may finish, and the gate adopts that stamped
result when it matches the world byte for byte. So bad worlds are caught one step earlier,
inside the authoring session, or end as a failed world job.

`world_check` (shared/taskformat/world_check.py) runs deterministically, with no model call, and
stops at the first failure:

1. **structure**: `repro/repro.sh`, `world_fix/fix.sh` and `world.json` exist; `world.json`
   has invariants and a counterexample; the Dockerfile uses an allowlisted base image.
2. **build**: the image builds with its base image pinned to a digest.
3. **repro_fails**: `repro.sh` exits 1 on the unfixed world, twice. A flaky counterexample
   is not a counterexample.
4. **repro_passes_with_fix**: after `fix.sh`, `repro.sh` exits 0, twice. The bug is fixable.

The hardness checks then run in `warn` mode: recorded on the world and passed to its
mutations, but never failing it.

- **symptom_visible / symptom_gone_with_fix**: the symptom an agent would try first shows on
  the world and goes away with the fix.
- **fix_not_local**: the fix spans 2+ files, or a dependent check breaks under a one-file
  patch.
- **wrong_fix_survives**: the plausible wrong fix clears the visible symptom but still fails
  the deep repro. That proves the trap is tempting.
- **limit_named** (a warning only): a function the fix changes names its own limit, such as
  `findTopLevelAssignments` or a docstring saying "only committed entries". An agent reads
  the name and has the bug.

**What a failure does:** the feedback goes to a world repair job (at most 2 per version),
then the task is rejected `invalid`. The only world-phase failure on record is
aws-agent-b1-t04 v1: the world was missing `world_fix/fix.sh` and `repro/repro.sh`, and
failed again on its retry.

**What it doesn't catch:** a world whose author wedges its own terminal. 47 world jobs were
cancelled at 23:52 on 2026-11-18 for that reason (heredoc wedges on pre-fix pods), not
because a gate failed.

## Task phase: is the task correct, and does it give the answer away?

The leak review is the strictest gate in the pipeline: it failed 157 drafts and rejected 20
of the 26 rejected tasks. The functional gates failed only 6 drafts, all in the early dev
batch aws-b1.

**Functional gates** (validator/gates.py, `validate` job), in order:

| Gate | Passes when | Catches |
| --- | --- | --- |
| structural | the bundle matches the Harbor layout and `task.toml` validates | missing files, a bad schema |
| lint | `harbor lint` has no blocking finding | e.g. a verifier sharing the agent's sandbox (a warning today) |
| oracle ×2 | `solution/solve.sh` passes the tests, twice | a broken reference or flaky tests |
| nop | an untouched container fails the tests | tests that pass with no work done |
| wrong_fix | the plausible wrong fix fails the tests | tests too weak to tell a real fix from a tempting wrong one |

The task author runs the same roles itself (`sde-task run`, in parallel) and stamps the
results. `validate` adopts a stamp that matches the bundle exactly, so a draft that fails
these is usually fixed before it is submitted. The 6 recorded failures were all `oracle1` on
aws-b1-t01 (reward 0, then no reward at all).

**Leak review** (`gate_leak`), three GLM-5.1 calls:

1. **Semantic review**: compares the instruction with the hidden fix, the counterexample and
   the world, and flags sentences that state them, directly or in other words. One direct
   finding, or two strong hints, is a leak.
2. **Blind prediction**: a second reviewer sees only the instruction and predicts the fix and
   where it goes.
3. **Judge**: compares that prediction with the real fix. Only `specific` (the right
   mechanism in the right module) is a leak. A textbook remedy without the location is
   `generic` and passes.

Guards on the judge: a placeholder prediction is retried, then marked inconclusive; in a
mutated world, predicting only the parent's already-solved fix doesn't count.

| Leak check | Drafts failed | Tasks rejected |
| --- | --- | --- |
| Blind prediction + judge | 131 (61 first drafts, 70 repairs) | 19 |
| Semantic review | 26 (17 first drafts, 9 repairs) | 1 (aws-w2-l1-t03) |

Examples of what it caught:

- **aws-w3-l4-t01** (performance): the instruction said "handle inputs that arrive through
  pipes or FIFOs, without reopening or seeking either input … within 256 MB of virtual
  memory". The semantic review flagged it as direct; both repairs were then predicted blind
  as "a single-pass, streaming, atomic-write pipeline".
- **aws-w1-l4-t04** (algorithmic): the blind reviewer predicted "the fix belongs in the POST
  /allocate handler", which was the real location.
- **aws-w1-l4-t01 v2** (algorithmic): a mutated greedy world, predicted blind three times as
  "replace the greedy first-fit selection in the scheduler's core planner".

## Evaluation phase: did the run measure the model?

Of 700 settled agent runs, 664 were scored (490 passes, 174 fails), 25 were `INVALID` and
retried, and 11 were skipped by early stopping. The Trace Parser then reviews every failure.
A failure it blames on the task sends the version to repair, even when the pass count is in
band.

**Classify** (evaluation/classify.py) decides whether a run counts at all. Each failure here
means "retry this slot", never "the model failed":

| Run result | Outcome |
| --- | --- |
| no exception, reward exactly 0 or 1 | `VALID_FAIL` / `VALID_PASS` |
| a Harbor exception (verifier timeout, sandbox or API error, missing reward file) | `INVALID`, retried |
| a reward other than 0 or 1, or no verifier result | `INVALID`, retried |
| the run used a different agent, model or reasoning effort than pinned | `INVALID` (`ConfigMismatch`) |
| the agent closed its own terminal (`exit` or `set -e` at the prompt) | the verifier's reward counts: the agent's own action, not the harness failing |
| the agent hit the 1-hour limit with reward 0 | `TIMEOUT`: no verdict until the Trace Parser rules (below) |

The 25 INVALIDs on record:

| Cause | Runs |
| --- | --- |
| agent timeout, 3600 s (before the timeout review existed) | 10 |
| the terminal died mid-run (tmux send-keys failed) | 9 |
| verifier timeout, 900 s (all on aws-w2-l1-t02) | 3 |
| `embedded null byte` in the agent's keystrokes | 2 |
| `ps` missing from the image | 1 |

**Infrastructure failure → rejected:** a slot gets 3 retries; still without a valid run, the
task is rejected `infra_unresolved`. If 5 runs come back INVALID within 10 minutes, the
executor stops claiming for 5 minutes, so a gateway or Docker outage doesn't burn every
task's retries. 6 tasks ended `infra_unresolved`: 5 in the early aws-b1 dev batch, plus
aws-agent-b1-t04 v4, whose create job was dead-lettered after 3 claims without settling.

**Timeout review** (live since deploy 1ea009b, 2026-11-19). A timeout doesn't count as the
model failing by itself. The Trace Parser reads the trace's last stretch: how long the
screen stayed unchanged, the largest command and where, and repetition. It then rules:

- **model failure** (wedged its own terminal, looped, abandoned the task, its own code hung)
  → `VALID_FAIL` with the reason recorded;
- **not the model** (still progressing, or slow infrastructure), or a review that fails or
  cites nothing → `INVALID` and retried.

On real traces:
- aws-agent-b1-t09 v4 run 2 sent a 26 KB heredoc at step 5, then saw the same screen for 201
  steps over 56 minutes → model failure.
- aws-agent-b1-t03 v1 run 3 was still fixing a real DP bug at its last step → retried.

A timeout that ends at reward 1 stays `INVALID`; 3 of the 10 historical timeouts did.

**Failure review**: one GLM-5.1 call per failed run, given the evidence (failing tests,
assertions, the agent's last commands), the instruction and the hidden spec. It attributes
the failure to `model` or `task` and must cite evidence ids; an uncited answer is
`unexplained`, kept but never trusted. Of 174 failures: 146 model, 9 task, 10 unexplained,
9 not yet reviewed.

**Bad task despite being in band → repaired, not accepted:** a failure blamed on the task
makes the version a `repair_ambiguity` revision whatever its pass count. 8 versions were sent
back this way; 2 of them were in band:

| Version | Passes | Why the task was blamed | Then |
| --- | --- | --- | --- |
| aws-w1-l1-t03 v1 | 1/5 | every failure was `assert 0 == -9`: the test harness's own SIGKILL arrived after `settle` had already finished; no invariant was violated | v2 repaired, 1/5, accepted |
| aws-w2-l1-t01 v1 | 3/5 | the agent's fix was correct, but the verifier didn't reset state left by earlier tests, a precondition the instruction never gave | v2 0/5, v3 in progress |
| aws-w1-l3-t02 v2 | 0/5 | tests required an exact error payload for a declined retry; the instruction never specified one | v3 in progress |
| aws-w2-l5-t03 v1 | 4/5 | a test expected HTTP 409 on cancelling an uncharged order; the instruction never mentioned cancellation rules | v2 repaired, 5/5 (too easy); v3 being mutated |

## Decision and revision: accept, revise or reject

The Trace Parser decides each version once, when all 5 slots are scored or the verdict is
already certain. It applies the decision as a compare-and-set on the task's stage, so two
copies of the service can't both decide. Of 150 tasks so far:

| Result | Tasks |
| --- | --- |
| accepted | 23 |
| rejected `invalid` | 20 |
| rejected `infra_unresolved` | 6 |
| rejected `exhausted` | 0 |
| cancelled by people | 51 |
| in progress | 50 |

| Evidence | Decision |
| --- | --- |
| 1-3 of 5 passed, no failure blamed on the task | **accepted**, published to `output/accepted/` with a `verdict.md` |
| a failure blamed on the task, at any pass count | revise `repair_ambiguity` on the same world |
| 4-5 of 5 passed (too easy) | world mutation: 1-2 operators aimed at what the passing runs did, chosen from the Trace Parser's pass review |
| 0 of 5 passed (too hard) | revise `easier` on the same world |
| still out of band at version 8 | **rejected** `exhausted` |
| a slot still without a valid run after 3 retries | **rejected** `infra_unresolved` |
| any draft still failing a gate after 2 repairs | **rejected** `invalid` |

**Early stopping** closes the remaining runs as `SKIPPED` when they can't change the
verdict:
- certain in band: at least 1 pass, and passes + remaining ≤ 3;
- certain too easy: 4 passes already.

It never stops on 0 passes, because one later pass would put the version in band. 11 runs
were skipped this way.

**Cancellation** is not a verdict: a cancelled task counts toward neither the yield nor the
rejections. 51 were cancelled on 2026-11-18, mostly to restart world builds on fixed pods.

## Edge cases the gates caught

The best catches are failures that looked like a model failure or a valid task and weren't:
- a test harness racing its own SIGKILL;
- a test asserting a requirement nobody stated;
- a terminal wedged by the agent itself.

Real cases from 2026-11-18, in pipeline order:

| Case | Gate | What it saw | Result |
| --- | --- | --- | --- |
| aws-agent-b1-t04 v1 | world job | the world lacked `world_fix/fix.sh` and `repro/repro.sh`, on the first try and on its retry | world failed |
| aws-iter1-t02, aws-iter1-t06 | world_check `limit_named` | a function named `findTopLevelAssignments`; a docstring saying "Only entries that have been committed": the code named its own bug | warning, fed to the mutation author |
| aws-b1-t01 | validate oracle | the reference solution scored 0, then produced no reward | repaired, later `infra_unresolved` |
| aws-w3-l4-t01 | leak: semantic | "handle inputs through pipes or FIFOs, without reopening or seeking … within 256 MB" names streaming | `invalid` |
| aws-w1-l4-t04 | leak: blind | a reviewer with only the instruction predicted "the fix belongs in the POST /allocate handler" | `invalid` |
| aws-agent-b1-t04 v2 | leak: judge (fixed) | the judge counted a prediction of the parent world's already-solved fix as a leak | rule added: the parent's fix is generic |
| aws-b1 batch | leak: blind (fixed) | placeholder predictions ("...") were judged "none" and passed the gate | now retried, then inconclusive |
| aws-iter2-t04, aws-agent-b1-t10 | classify | the agent typed `exit` at its own prompt; the terminal closed after the verifier had already scored 1 | now scored, no longer re-paid as INVALID |
| aws-agent-b1-t09 v4 run 2 | timeout review | a 26 KB heredoc at step 5, then 201 steps on the same screen over 56 min | model failure (`VALID_FAIL`) |
| aws-agent-b1-t03 v1 run 3 | timeout review | still fixing a real DP bug at its last step | not the model: retried |
| aws-w1-l1-t03 v1 (1/5, in band) | failure review | `assert 0 == -9`: the test's own SIGKILL arrived after `settle` finished; no invariant broken | repaired; v2 accepted at 1/5 |
| aws-w2-l1-t01 v1 (3/5, in band) | failure review | the agent's fix was right; the verifier didn't reset state left by earlier tests | repaired |
| aws-w1-l3-t02 v2 | failure review | tests required an exact error payload the instruction never specified | repaired |
| aws-w2-l5-t03 v1 | failure review | a test expected HTTP 409 on cancelling an uncharged order; not in the instruction | repaired |
| aws-iter1-t01 v1 | failure review | tests inserted raw SQL into a table the instruction never named | repaired; the explanation was cut at 303 characters by an unescaped quote |
| aws-w2-l1-t02 v1 | classify | the verifier timed out at 900 s on 3 runs | `INVALID`, retried |

## Gaps and recommendations

The biggest gap: test defects reach the agent runs. A test that asserts something the
instruction never asked is caught only after 5 paid runs, by the failure review, and only if
a run trips it. Each row is a case no gate caught early, with a proposed check.

| Gap | Seen in | Proposed check |
| --- | --- | --- |
| Tests assert requirements the instruction never states (an exact payload, an HTTP 409, a raw table name) | aws-w1-l3-t02, aws-w2-l5-t03, aws-iter1-t01 | at validate, one model call maps each test assertion to the instruction sentence it checks; an assertion with no sentence fails the draft |
| Generated test inputs break the instruction's own rules (a job listed as its own prerequisite, where the instruction requires an acyclic graph) | aws-agent-b1-t03 (3 false fails) | the test generator asserts every stated rule on its inputs; plus the mutation-testing gate in docs/TODO.md (at least 70% of small bugs in the fixed code must fail the tests) |
| Timing races in the test harness pass the oracle twice but fail an agent's slower or faster code | aws-w1-l1-t03 (`assert 0 == -9`) | run the oracle under CPU load, or with a delay injected around fault points |
| A version can be accepted on failures the reviewer couldn't explain (10 `unexplained` so far) | every shipped task should come with the reason it fails | accept only when at least one failure is attributed to the model with cited evidence |
| The failure review's conclusion is often a bare label ("task", "model"), so the repair gets less to work from | the 8 task-blamed versions | a decision for the user: the Trace Parser's verdicts stand, so this would change only the wording asked of the review |
| The blind leak check causes 19 of 20 `invalid` rejections, and nobody has measured whether the drafts it rejects would have been too easy | 131 blind failures vs 26 semantic | a decision for the user, who cancelled a shadow run of this on 2026-11-18: record the blind verdict without enforcing it on a small batch, and compare pass counts |
| A timeout that ends at reward 1 stays `INVALID` and is re-paid | 3 of the 10 historical timeouts | count it as a pass when the timeout review finds no wedge, or keep retrying (a decision for the user) |
| The hardness checks never block a world (`warn` mode), so a world with a one-file fix still reaches task authoring | all 150 tasks | enforce `fix_not_local` and `wrong_fix_survives` for performance and algorithmic worlds, where too easy is the common failure |
