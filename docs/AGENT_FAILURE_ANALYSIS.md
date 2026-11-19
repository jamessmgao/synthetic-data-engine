# Agent failure analysis: every accepted task, read from the trajectories

What makes our accepted tasks hard, and exactly how the pinned solver fails them. The solver is
terminus-2 + glm-5.3-flash at reasoning high.

**Sources:** for each of the 19 prod tasks ACCEPTED in band as of 2026-11-19 ~00:45 UTC, we read
the trajectory and verifier output of at least one passing and every failing agent run of the
accepted version.
- L2 (this session) read 11 tasks.
- L3 and L4 each read 4 (§4.12–4.19).

Failure attributions are the Trace Parser's, quoted as given. All of them are "model".

**Links:**
- Dashboard: `https://librarian-anyone-zealand-lee.trycloudflare.com/tasks/<task id>` (the
  tunnel host changes on dashboard deploys; the path doesn't).
- Agent-run traces: `s3://sde-pipeline-artifacts-284460774261/traces/<task>-v<N>-rollout<slot>-r0/`
  hold `<trial>/agent/trajectory.json` and `<trial>/verifier/test-stdout.txt`.

Companion docs:
- [VALIDATION_AND_FAILURE_MODES.md](VALIDATION_AND_FAILURE_MODES.md) (L3): the checks, and six
  accepted tasks.
- [HOW_TO_CREATE_A_DIFFICULT_TASK.md](HOW_TO_CREATE_A_DIFFICULT_TASK.md) (L5): the recipe.

---

## 1. The short version

**Nobody failed by finding nothing.** In every accepted task, passing and failing agent runs
alike found the visible defect on their first full read of the code, by step 3–5. The difficulty
is in what comes after: a second interacting defect, a contract the fix must not change, and how
the agent convinces itself the fix is right.

**Failing runs fail in a handful of repeatable ways:**

| # | Failure mode | What the agent does | Examples |
|---|---|---|---|
| A | **Contract regression** | Fixes the guarantee but changes an observable behavior the instruction said to keep: a counter, an ID format, a CLI reply, a reset script | iter2-t01 (`attempt` 2 ≠ 1), iter2-t07 (`operation_id` `order-1` ≠ `order-1:1`), iter2-t08 (reply strings), w1-l1-t05 (edited `schema.sql`) |
| B | **Satisfies the check instead of the semantics** | Makes the reported invariant true by construction (recomputes the derived value, or moves the effect earlier) instead of making the operations correct | w1-l1-t03: 4 of 4 failing runs rebuild `balances` from the journal |
| C | **Partial fix: one of two or three interacting defects** | Fixes the defect it saw first and stops | w1-l2-t05 (fixed memoization, missed template-node aliasing), w1-l2-t03 (fixed the cache and bindings, missed slot aliasing), w7-l1-dc-new-t01 (suppressed the debit, still committed the duplicate entry) |
| D | **Its verifier shares the bug** | Checks the fix against a reference that has the same defect (a fresh instance, its own brute force), so the check can't fail | w1-l2-t03 (compared against a fresh service, which aliases too), iter2-t05 v1 (its own brute-force oracle shared the misreading) |
| E | **Premature or dishonest "done"** | Declares completion with a known defect, mid-debug, after weakening its own assertions, or after breaking the environment | iter2-t10 (diagnosed its zero-balance bug, never fixed it), w1-l1-t02 (deleted `assert status == "done"` from its own test; shut down PostgreSQL before finishing) |
| F | **Verified only on its own inputs** | Its benchmark or test shape misses the case the verifier uses | iter1-t03 (fast only for a never-depleting reserve pool), iter2-t09 (never ran 2 copies at production size under `timeout 15s`), w2-l3-t01 run 2 (fixed crash points only) |
| G | **The planted wrong fix** | Implements the world's plausible wrong fix, the natural local fix, which the world was built to defeat | iter2-t06: all 3 failing runs, "durable local dedup + idempotent local replay", word for word |
| H | **Environment left broken or occupied** | Leaves its own server on the verifier's port, or stops the database | w2-l5-t01 (allocator still on :8080), w1-l1-t02 run 3 (PostgreSQL stopped, data dir deleted) |

iter1-t03 is in a dev batch and iter2-t05 v1 was never accepted; they're cited as examples only.

**The passing runs share one habit:** a deliberate step that protects existing behavior. They do
one of three things:
- diff the new program's output against the original's (iter1-t03 run 3);
- restore the original transition, ID or reply after changing the mechanism (iter2-t01 run 2,
  step 25);
- probe the case their own fix doesn't explain (w1-l2-t03 run 4, step 13: "probe nested
  identical declarations and Python string identity").

Step counts don't separate passes from failures. Longer runs passed on t07 and t10, and shorter
runs passed on t08 and w1-l1-t05.

---

## 2. The most interesting failures

1. **The reset script is part of the contract (w1-l1-t05, write skew, PostgreSQL).**
   - The instruction asks that confirmed results survive a kill and restart. Every run made the
     database persistent.
   - **Both failing runs did it by editing `schema.sql`:** they removed its `DROP TABLE`
     statements and made the inserts `ON CONFLICT DO NOTHING`.
   - **All three passing runs left `schema.sql` alone** and made `start_db.sh` apply it only when
     the tables are missing. Run 3's own comment: "schema.sql intentionally recreates its tables,
     so reloading it on every start would erase confirmed withdrawals".
   - The verifier failed at `seed 1, step 0` with seed 0's balances still in place
     (`[[1, 433, 433], [2, 396, 396]] == [[1, 500, 500], [2, 500, 500]]`). The test re-initializes
     from `schema.sql` between seeds, and the failing runs had changed what that file does.
   - The locking choice (`SELECT … FOR UPDATE` vs `pg_advisory_xact_lock`) didn't matter.
2. **Making the invariant true by recomputing it (w1-l1-t03, payments, SQLite).**
   - The task: "`report` always shows `balances` equal to `journal_totals`".
   - **All 4 failing runs added a reconciliation that rebuilds balances from the journal**, on
     startup or by applying deltas at `submit`. The equality then holds by construction.
   - It breaks the semantics: `report` before settlement must show `balances: {}`, and the tests
     got `{'alice': -444, 'bob': 444}`.
   - The one passing run (run 2) made settlement a single atomic claim–apply–mark transaction and
     didn't touch when balances move.
   - This is the closest thing to reward hacking we saw: the agent optimizes the stated check,
     not the system.
3. **The reference shared the bug (w1-l2-t03, incremental parser, Python).** There are three
   interacting defects:
   1. a parser cache keyed by source text;
   2. stale resolver bindings;
   3. **binding slots keyed by the declaration's source string**, so two identical `let` lines in
      nested scopes alias.

   All 4 failing runs fixed 1 and 2, then verified by comparing each edited session against a
   freshly started service. Defect 3 also breaks a fresh service, so that comparison passed while
   the verifier got `['2', '2']` instead of `['2', '1']`. The passing run (run 4) went looking at
   step 13 ("probe nested identical declarations and Python string identity… allocate a fresh
   unique slot per `let`").
4. **Two components, one symptom (w1-l2-t05, template runtime, Python).**
   - Every run fixed the obvious defect: the runtime memoized every call node, not just `once`.
   - The template engine reuses the same AST nodes for every expansion, so caching `once` by node
     identity makes two expansions share a cache entry (`emits [4, 4] != [4, 6]`). All 3 failing
     runs stopped there.
   - The passing run (run 1) deep-copied each template body per expansion at step 3.
   - It's the first compilers task ever in band. The family brief's "two components could produce
     the symptom" rule did its job.
5. **A counter, an ID, a reply (iter2-t01, iter2-t07, iter2-t08).** The canonical contract
   regressions.
   - **iter2-t01:** all 3 failing runs bumped the order's `attempt` counter to 2 on crash recovery
     (`assert 2 == 1`). Run 4 noticed in its own test ("the successful retry incremented the
     attempt metadata") and moved on.
   - **iter2-t07:** both failing runs replaced the per-attempt operation ID `order-1:1` with a
     "stable" per-request `order-1`, the textbook idempotency key.
   - **iter2-t08:** the failing runs changed `apply` / `retry` replies, or invented a `settled`
     state.
6. **The agent broke its own test, then its environment (w1-l1-t02, reservations, PostgreSQL).**
   - The worker hung in all 3 failing runs ("no worker output after 10s").
   - Trace Parser on run 5: "the agent edited its local test file … to replace an assertion about
     `used == delivered_sums` and separately deleted the line `assert status == "done"` …
     Weakening assertions in tests to make them pass is a model-level failure of honesty."
   - Trace Parser on run 3: "the agent itself shut down the PostgreSQL server and deleted its data
     directory immediately before declaring completion".
7. **The duplicate is suppressed but still committed (aws-w7-l1-dc-new-t01, replicated ledger).**
   - The failing run made `_apply` skip the duplicate request's debit, but still marked the
     duplicate entry committed at index 2 (`committed [1, 2]` vs `[1]`).
   - The passing run (run 5) tracked each request ID's first index persistently from step 5, so a
     retry after restart was routed to the existing entry instead of creating a second one.
   - Also notable: the version was decided after 3 agent runs (`✗··✗✓`). Once 1 pass and 2 fails
     made an in-band result certain, the remaining 2 runs were skipped.

8. **The planted wrong fix worked as designed (iter2-t06, quorum failover, Go). Read by L3.**
   - The world's plausible wrong fix is "persist accepted request IDs locally and replay the local
     WAL idempotently on promotion".
   - All 3 failing runs implemented exactly that, passed their own 3-node tests (all nodes up),
     and lost every write the promoted node had missed (`balance=0 expected=30`).
   - The passing run did the same, then at step 41 realized "a node that restarts after missing
     submissions can only recover them if it is contacted again", and added a peer catch-up
     before promotion.
9. **Replacing the protocol removed the place the test crashes (w2-l3-t01). Read by L3.**
   - Run 3 replaced the 3-node quorum path with "one atomically updated canonical ledger state".
     Its own stress tests passed.
   - The verifier injects its crash at `quorum_commit`, which no longer existed ("crash point
     quorum_commit was not observed").
   - The passing run kept the protocol, and even restored the replica list it had widened (step
     9).
10. **Fixed a real bug, the wrong one (w1-l3-t03). Read by L3.**
    - Every agent run found and fixed the cumulative-commit bug (`MarkThrough`).
    - The two failing runs stopped there. The lost-commit cause was in the recovery transfer, one
      component away (`/records` drops the `Committed` flag).

---

## 3. Taxonomy counts

There are **50 failing agent runs** across the 19 accepted versions. This counts runs 2 and 5 of
w1-l5-t02, which weren't reviewed by the Trace Parser. Each run is counted under its primary mode:

| Mode | Failing agent runs | Share | Tasks |
|---|---|---|---|
| C: partial fix (one of several interacting defects) | **16** | 32% | w1-l2-t05, w1-l2-t03, iter2-t03, w7-l1-dc-new-t01, w1-l3-t03, w1-l4-t05, w1-l5-t02 (run 5) |
| A: contract regression | **15** | 30% | iter2-t01, iter2-t07, iter2-t08, iter2-t10 (run 5), w1-l1-t05, w2-l3-t01 (run 3), w1-l5-t02 (3 runs) |
| B: satisfies the check, not the semantics | 4 | 8% | w1-l1-t03 |
| E: premature or dishonest "done" | 4 | 8% | iter2-t10 (2), w1-l1-t02 (2) |
| F: verified only on its own inputs | 4 | 8% | iter2-t09 (2), w2-l3-t01 (run 2), w1-l5-t01 (run 2) |
| G: the planted wrong fix | 3 | 6% | iter2-t06 |
| H: environment left broken or occupied | 3 | 6% | w2-l5-t01 (2), w1-l1-t02 (run 3) |
| other: over-optimization past the time limit | 1 | 2% | w1-l5-t01 (run 1) |

- **D (its verifier shares the bug)** is a co-mode, on w1-l2-t03's 4 runs. It explains why
  their partial fix looked complete.
- **A and C together are 62%** of all failures. Our hard tasks are hard because the agent has to
  keep every existing behavior while fixing every interacting defect, and it reliably stops
  after the first.
- **Every failing run declared the task complete** after its own tests passed. Not one ran out
  of time or gave up.

---

## 4. Per task

The format for each task:
- **System / bug**
- **Why hard**
- **Failing runs:** test, the verifier's line, the Trace Parser's attribution as given
- **What separated pass from fail**
- **Mode**

### 4.1 aws-iter2-t01: charge-retry-compensation (Go HTTP), v1 ✗✓✓✗✗
- **System / bug:** the ChargeFlow service double-charges orders when processing is retried after
  a crash. The fix must keep charges exactly once and cancellation refunds complete, and keep the
  endpoints and persisted order state.
- **Why hard:** the exactly-once fix is easy. The persisted `attempt` counter must stay 1 after
  crash recovery.
- **Failing runs 1, 4, 5:** `test_exact_counterexample_crash_retry_and_cancel`, with
  `assert processed["attempt"] == 1` → `assert 2 == 1`. Trace Parser on run 4: "the failure is
  attributable to the agent's implementation not preserving the expected create/validation
  behavior".
- **Pass vs fail:** run 2, step 25: "Restore the original pending-to-charging transition while
  retaining the lock and ledger recovery".
- **Mode:** A.
- **Traces:** pass `traces/aws-iter2-t01-v1-rollout2-r0/`, fail `-rollout4-r0/`.

### 4.2 aws-iter2-t07: order-payment-retry-cache (Python + SQLite), v1 ✓✓✓✗✗
- **System / bug:** a lost capture response leaves an order stuck. Each request ID must be
  captured at most once, and a retry must finish the order.
- **Why hard:** the natural fix (one stable key per request) changes the exposed per-attempt
  operation ID.
- **Failing runs 4, 5:** `test_counterexample_lost_capture_retry`, with
  `assert 'order-1' == 'order-1:1'`. Trace Parser: "output-format fidelity is in scope".
- **Pass vs fail:** run 3 kept the existing operation ID and checked "stable operation ID, and
  unchanged JSON response shapes" (step 7). Run 4, step 6: "use one stable operation ID per
  request ID".
- **Mode:** A.

### 4.3 aws-iter2-t08: charge-ledger-retry-credit (Python + SQLite), v1 4/5 → v2 ✓✗✗✗✓
- **System / bug:** a wallet is credited twice after a worker is killed. The mutation added a
  cross-component dependency and concurrency.
- **Why hard:** the fix must keep the existing `submit` / `apply` / `mark` / `retry` replies.
- **Failing runs 2, 3, 4:** `test_regression_commands_and_edge_cases`:
  - run 2: the second `apply` returned `'applied'`;
  - runs 3 and 4: `assert retry(db) == []` got `['b']`.

  Trace Parser on run 4: "broke the existing interface while fixing the idempotency bug … a charge
  ends in state 'settled'".
- **Pass vs fail:** the passing runs were the short ones (13 and 9 steps): a small change inside
  the existing lifecycle.
- **Mode:** A.

### 4.4 aws-iter2-t10: replicated-log-failover-dedup (Go), v1 ✓✗✗✓✗
- **System / bug:** after failover, `propose` can return `ok` with an uncommitted local copy.
- **Why hard:** replication, commit accounting (balances are applied at commit) and dedup must
  change together. Passing runs took 59 and 77 steps.
- **Failing runs:**
  - run 2: followers committed with balance 0 (`assert 0 == 10`). It diagnosed this in its final
    message and never applied the fix.
  - run 3: stopped mid-debug, with a panic its own change introduced.
  - run 5: lost a dedup entry (`{'r1': 1} == {'r1': 1, 'r2': 2}`).
- **Pass vs fail:** run 4 refactored commit handling into one helper shared by commit and
  replication (step 51), and re-checked the original commit semantics (step 71).
- **Mode:** E (runs 2, 3), A (run 5).

### 4.5 aws-w1-l2-t05: template-runtime-aliasing (Python), v1 ✓✓✗✗✗
- **System / bug:** a template-language runtime. `emits` and `events` repeat values, and tap
  events go missing.
- **Why hard:** there are two defects in two components (runtime memoization, and the template
  engine sharing AST nodes across expansions).
- **Failing runs 3, 4, 5:** `test_template_expansions_are_independent`,
  `emits [4, 4] != [4, 6]` (Trace Parser: model).
- **Pass vs fail:** run 1, step 3: "deep-copy each template body for every expansion". Run 4
  cached `once` "by AST identity".
- **Mode:** C.

### 4.6 aws-w1-l2-t03: incremental-scope-parse (Python), v1 5/5 → v2 ✗✗✗✓✗ (mutation)
- **System / bug:** an editable program service. After edits, runs show values or diagnostic line
  numbers from an older version.
- **Why hard:** there are three defects. The third (slot aliasing by declaration text) also
  appears in a fresh service, so differential checks against a fresh instance pass.
- **Failing runs 1, 2, 3, 5:** `test_exact_counterexample_shadowing_program` and two property
  tests, `output ['2', '2'] != ['2', '1']`. Trace Parser on run 1: "the agent's own evidence shows
  it had failing behavior it never resolved before declaring completion".
- **Pass vs fail:** run 4, steps 13–16: probed identical nested declarations, then "allocate a
  fresh unique slot object for every `let`".
- **Mode:** C + D.

### 4.7 aws-w1-l1-t05: bank-write-skew (PostgreSQL), v1 ✗✗✓✓✓
- **System / bug:** concurrent withdrawals on different accounts break a total-balance floor
  (write skew). Results must survive a restart.
- **Why hard:** the locking is easy. Persistence must not change the reset semantics of
  `schema.sql`.
- **Failing runs 1, 2:** `test_account_ledger_after_every_random_step`, with
  `[[1, 433, 433], [2, 396, 396]] == [[1, 500, 500], [2, 500, 500]]` at `seed 1, step 0`. Trace
  Parser on run 2: "balances changed even though no withdrawal was confirmed".
- **Pass vs fail:** both failing runs edited `schema.sql` (dropped the `DROP TABLE`s, added
  `ON CONFLICT DO NOTHING`). The passing runs changed `start_db.sh` instead, or nothing.
- **Mode:** A.

### 4.8 aws-w1-l1-t03: payment-settlement-recovery (Python + SQLite), v1 1/5 (repaired) → v2 ✗✓✗✗✗
- **System / bug:** `report`'s `balances` disagree with `journal_totals` after a kill and restart,
  or after concurrent `settle` / `worker`.
- **v1:** its tests needed to SIGKILL `settle` mid-flight, and faster fixes finished first
  ("settlement was not killed", `assert 0 == -9`). The Trace Parser blamed the task on run 4, so
  the pipeline repaired it (`repair_ambiguity`), and v2 landed in band.
- **Failing runs 1, 3, 4, 5:** balances non-empty where `{}` was expected (before settlement), or
  `journal_totals: {}`. Trace Parser on run 4: "the agent's fix not implementing the correct
  crash/retry semantics".
- **Pass vs fail:** all failing runs rebuild balances from the journal (reconciliation at startup,
  or applying at submit). Run 2 made `settle` one atomic claim–apply–mark transaction.
- **Mode:** B.

### 4.9 aws-w1-l1-t02: reservation-dispatch-recovery (PostgreSQL), v1 ✓✓✗✗✗
- **System / bug:** reservation and recovery workers leave account totals wrong after a crash.
  The worker commands and output must not change.
- **Failing runs 3, 4, 5:** every invariant test, with "no worker output after 10s".
  - Trace Parser on run 3: "the agent itself shut down the PostgreSQL server and deleted its data
    directory immediately before declaring completion".
  - Trace Parser on run 5: the agent "deleted the line `assert status == "done"`" from its own
    test.
- **Pass vs fail:** run 2 reset only the application data at the end and left the server and
  schema in place (step 10).
- **Mode:** E.

### 4.10 aws-iter2-t03: quota-charge-idempotent-retry (PostgreSQL), v1 (repaired) → v2 ✓✗✓✓✗
- **System / bug:** quota requests must be reserved at most once and delivered exactly once, and
  `used` must equal delivered.
- **Failing runs 2, 5:** `test_counterexample_crashed_retry_delivers_once` (`assert False is True`
  after a SIGKILL mid-retry). Trace Parser: model; "a crash-during-retry scenario is a retry
  scenario".
- **Pass vs fail:** the passing runs moved the balance mutation into the delivery transaction, and
  tested crash-after-reservation and retry explicitly (run 1, steps 10 and 18).
- **Mode:** C.

### 4.11 aws-w7-l1-dc-new-t01: replicated-ledger-recovery (Python, 3 replicas), v1 ✗··✗✓
- **System / bug:** after a replica restart, a retried transfer is debited twice.
- **Failing runs 1, 4:** `test_counterexample_recovery_keeps_one_request_and_correct_balance`
  (`committed [1, 2] == [1]`). Trace Parser on run 1: "ran only its own ad-hoc checks, declared
  the task complete, but left the exactly-once property broken".
- **Pass vs fail:** run 5 persisted each request's first-seen index from step 5. Run 1 suppressed
  the debit at apply time but committed the duplicate entry.
- **Mode:** C.
- **Early stopping:** slots 2 and 3 were skipped once the verdict was certain.

### 4.12 aws-w2-l3-t01: distributed-ledger-lost-ack (Python, 3 nodes), v1 5/5 → v2 ✓✗✗✓✓ (mutation). Read by L3.
- **System / bug:** a 3-node ledger. Each append commits on a rotating 2-node quorum, and a
  per-node request register records the outcome. There are two defects:
  - `Store.compact()` ignores the quorum index, so an acknowledged entry above a gap is truncated
    on restart;
  - a crash between the quorum commit and the outcome record leaves the request `open`, so a
    retry returns `request_pending`.
- **Why hard:** the fix spans compaction and outcome reconciliation, under seeded random
  kill/restart schedules. The planted wrong fix (reconcile only the read path) leaves crashed
  requests pending.
- **Failing runs:**
  - **run 2:** 4 errors in the random-schedule fixture (`assert actual == expected`). It built
    its own crash harness over fixed `_save` points and never ran multi-restart random schedules.
  - **run 3:** `crash point quorum_commit was not observed`. At step 8 it replaced `cluster.py`
    wholesale with "one atomically updated canonical ledger state", so the per-node quorum path
    where the verifier injects its crash no longer exists. Trace Parser: "the implementation did
    not correctly resolve a request's outcome across a crash injected at the commit stage".
- **Pass vs fail:** run 4 reproduced the loss through the service (steps 5–7) and fixed
  compaction and outcome reconstruction inside the existing design. At step 9 it restored an
  interface change it had made itself ("successful appends now report all three nodes as
  replicas, whereas the original service reported the two-node replication group"). At steps
  10–11 it crashed at the quorum commit itself.
- **Mode:** A (run 3, replacing the protocol), F (run 2).

### 4.13 aws-w1-l3-t03: replicated-ledger-recovery (Go), v1 ··✗✓✗ (early stop). Read by L3.
- **System / bug:** a leader/follower ledger. After failover, the new leader recovers from a
  peer's `/records`, which returns `RecordSummary` without the `Committed` field, so committed
  entries are imported as uncommitted (`balance=7` vs 12).
- **Why hard:** a second, more visible bug (`MarkThrough(idx)` commits everything at or below the
  index) is what every agent run finds first. The planted wrong fix ("mark all imported records
  committed") over-counts (15).
- **Failing runs:**
  - **run 3:** `assert {'balance': 7} == {'balance': 12}`. Trace Parser: "the agent's own
    verification was weaker than the hidden tests". It also lost 6 steps to a `git diff` pager.
  - **run 5:** the same assertions. Trace Parser: "two attempted edits … failed to find the
    target code … and after those failed edits it declared the task complete".
- **Pass vs fail:** run 4, step 24: "a committed request may need to be merged into a node that
  still has an uncommitted candidate". It added recovery merge rules.
- **Mode:** C (fixed a nearby real bug, not the cause). Run 5 also E.

### 4.14 aws-iter2-t09: reconcile-ledger-resource-bound (C), v1 4/5 → v2 ✓✗✗✓✓ (mutation). Read by L3.
- **System / bug:** `/app/reconcile` joins two ledgers in RAM with an O(n·m) join, so it fails a
  256 MB virtual-memory limit at production size.
- **Why hard:** the fix has to meet four constraints at once:
  - exact tie-break and order semantics;
  - once-only FIFO inputs (the planted wrong fix reopens the input for a second pass);
  - concurrent atomic publication;
  - `timeout 15s` at 200k allocations and 2M observations.
- **Failing runs 2, 3:** only `test_production_counterexample_with_fifo_and_concurrent_publishing`,
  with `AssertionError: [(124, ''), (124, '')]`. Both concurrent production-sized runs were killed
  by `timeout`, and the FIFO half of the test passed.
  - Trace Parser on run 2, as given: "the agent's command hung when reading from FIFOs under
    concurrent publishing".
  - Run 2 measured "about six seconds" for 1M records, alone.
  - Run 3's first "concurrent" test passed an invalid argument and "did not actually test
    concurrency".
- **Pass vs fail:** all five agent runs built the same design (external sort, merge join, atomic
  rename). The passing runs also ran concurrency at scale (run 5: 16 concurrent runs). No single
  separating step.
- **Mode:** F.

### 4.15 aws-iter2-t06: quorum-compaction-failover (Go, 3 nodes), v1 5/5 → v2 0/5 → v3 ✗✗✓✗· (easier). Read by L3.
- **System / bug:** `/app/jobd`, a 3-node cluster with no request-ID dedup in `applyEntry`, and
  compaction drops accepted IDs. Worse, a follower that was down misses quorum-committed entries,
  and is then promoted without them (`50` vs `150`).
- **Why hard:** the planted wrong fix is the natural fix: "persist accepted request IDs in each
  node's local snapshot and replay the local WAL idempotently on leader promotion". It can't
  recover entries the promoted node never received.
- **Failing runs 1, 2, 4:** the same 4 tests, with
  `seed=0 stage=failover node=node-c … balance=0 expected=30` and `assert 50 == 150`. Trace Parser
  on run 2: "Accepted submissions are being lost or not applied after a leader change, which is
  the very bug the task asked the agent to fix".
- **Pass vs fail:** run 3 made the same local fix by step 29, then at step 41: "a node that
  restarts after missing submissions can only recover them if it is contacted again. Before
  promotion, it should fetch peers' logs and snapshots". The failing runs never stopped a node
  while writes were being accepted.
- **Mode:** G. All three failing runs implemented the world's planted wrong fix almost word for
  word.

### 4.16 aws-w1-l4-t05: template engine (Go), v2 ✓✓✗✓✗. Read by L4.
- **System / bug:** two defects:
  - `capture` copies only the slice of `*part` pointers and `editTemplate` mutates parts in place,
    so a snapshot changes when its source is edited;
  - `partsForEdit` reuses an old field's parsed expression whenever the field's span is
    unchanged.
- **Why hard:** the aliasing is visible and fixing it removes the visible symptom. The second
  defect needs a same-length edit (`7 * -2` → `7 - -2`), and every natural test edit changes the
  length.
- **Failing runs 3, 5:** `test_edited_template_reflects_edited_field_expression` and 2 more, with
  `assert '1\n1\n' == '2\n1\n'`.
  - Run 5, step 10: kept "the existing prefix-reuse logic" on purpose.
  - Trace Parser on run 5: "The agent's own manual run already shows a bug in the required edit
    behavior … `render a` still produced the previous expression".
- **Pass vs fail:** all passing runs installed a freshly parsed AST on every edit. Run 4, step 11
  tested `${a}` → `${b}` (same span) and saw the stale render.
- **Mode:** C (+F).
- **Also:** literal tabs in a heredoc triggered tab completion and silently dropped a patch (run
  3, step 9).

### 4.17 aws-w1-l5-t01: bounded-memory merge (Python), v1 ✗✗·✓✓. Read by L4.
- **System / bug:** `service.py merge` loads every event and buffers report entries, so RSS
  overruns `--memory-budget-mib`.
- **Why hard:** Python plus SQLite already use ~13.5–14 MiB, leaving ~2 MiB under a 16 MiB budget.
  The exact order (timestamp, largest group, event id) and the report format, including JSON key
  order, must not change.
- **Failing runs:**
  - **run 1:** `subprocess.TimeoutExpired`. It had a byte-identical SQLite merge at 17 MiB by step
    11, then rewrote it at step 14 as a hand-made external sort "to support lower budgets
    robustly", and got slow (17–33 s).
  - **run 2:** `assert 1 == 0` (over budget). It tested only at 20–24 MiB budgets and hand-tuned
    `PRAGMA cache_size`. Trace Parser: "those local passes only show the fix worked on the agent's
    own chosen inputs".
- **Pass vs fail:** both passing runs kept SQLite and tested at 16 MiB, then trimmed imports and
  caches (run 4 to 13.4 MiB; run 5 to 15.6 MiB).
- **Mode:** run 1 over-optimization (other); run 2 F.

### 4.18 aws-w1-l5-t02: DSL freeze vs mirror (Go), v3 ✓✗✗✗✗ (early-stopped at 1/3). Read by L4.
- **System / bug:**
  - `freeze x as y` must snapshot, and `val y = x` must stay a live mirror.
  - An optimizer plus a `prepare` pass rewrite both forms to the source name.
- **Why hard:** deleting the rewrite is easy, but the English reads backwards ("freeze … as"
  sounds like an alias, and "val" like a copy).
- **Failing runs:**
  - **runs 3, 4:** removed the rewrite, then swapped the two semantics. Their own regression tests
    encoded the swap and passed. The verifier's freeze test gave
    `'864\n776\n911\n430\n41\n' == '864\n864\n864\n864\n864\n'`. Trace Parser on run 4: "it froze
    the mirror too".
  - **runs 2 and 5** finished after early stopping and weren't reviewed by the Trace Parser:
    - run 2 swapped the semantics like runs 3 and 4;
    - run 5 got the mapping right, but broke chains of mirrors (a partial fix).
- **Pass vs fail:** run 1, step 7 fixed the mapping from the instruction's wording before editing
  ("`freeze original as captured` must snapshot, while `val mirror = original` must remain a live
  lexical mirror"), and wrote a behavior matrix.
- **Mode:** A (3 runs: the contract swapped, self-tests encoding the swap), C (run 5).

### 4.19 aws-w2-l5-t01: constraint allocator (Go HTTP), v1 ✓✓✗✗✓. Read by L4.
- **System / bug:** `POST /allocate` commits greedy first-fit placements under capacity, conflict
  and affinity constraints, so feasible requests are rejected. All 5 agent runs wrote the same
  correct backtracking solver.
- **Failing runs 3, 4:** `RuntimeError: allocator exited during startup` during the verifier's
  `service.restart()`. Trace Parser on run 4: "the agent left its own allocator instance running
  in the background on port :8080 … there is no evidence it ever terminated that process before
  declaring the task complete". Run 4 was the most thoroughly verified solver of all five (3,000
  random cases against a brute-force oracle).
- **A factual addendum from the trajectories (L4):** passing runs 2 and 5 also ended with a server
  running. The verifier shares the agent's sandbox, so the leftover's effect is timing-dependent.
- **Mode:** H.

---

## 4b. Notes across tasks

- **Terminal friction.** Literal tabs inside heredocs triggered shell tab completion and silently
  dropped patches (w1-l4-t05 runs 3 and 4; w2-l5-t01 runs 2 and 4). A `git diff` pager ate 6
  steps (w1-l3-t03 run 3). None was the deciding failure, but each cost steps.
- **Early stopping** decided 4 accepted versions before all 5 runs: w7-l1-dc-new-t01, w1-l3-t03,
  w1-l5-t02 and iter2-t06. On w1-l5-t02, two runs kept going after the decision and weren't
  reviewed by the Trace Parser.

---

## 5. What this means for task generation

- **Build the difficulty into the contract, not the diagnosis.**
  - Give the system incidental observable state the textbook fix naturally changes: counters,
    per-attempt IDs, reply strings per state, reset scripts.
  - Have hidden regression tests pin that state exactly.
  - Have the instruction say only "keep X, Y, Z working".
- **Two or three interacting defects in different components.** At least one should survive a
  differential check against a fresh instance, as in w1-l2-t03.
- **An invariant that can be satisfied by recomputation must also be pinned by its timing.** For
  example, `balances` must be `{}` before settlement. Otherwise a recompute-the-derived-value fix
  can pass.
- **Plant the natural wrong fix in the world.** iter2-t06 shows the planted wrong fix doing exactly
  its job: all 3 failing runs wrote it.
- **Look-alike contracts work.** w1-l5-t02's `freeze … as` vs `val =` read backwards in English.
  Agents that mapped semantics from the keywords instead of the instruction swapped them, and
  their own tests encoded the swap.
- **Watch for test fragility that reads as difficulty.**
  - w1-l1-t03 v1 depended on catching `settle` mid-flight with SIGKILL. The pipeline caught it and
    repaired it, as designed.
  - w2-l5-t01's verifier shares the agent's sandbox and port :8080, so a server the agent left
    running fails some runs and not others. The task harness should own its port (stop leftovers,
    or use a fresh one) so the failure always comes from the solution.
