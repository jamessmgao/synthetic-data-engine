# TODO (deferred, low priority)

Work we decided to postpone. Each item says why it waits and when it would matter.

## Prebuilt base images per environment

**What:** an `sde-base-<env>` image per environment in `config/base_images.json`, built from the
pinned digest with that environment's toolchain and test dependencies preinstalled (python +
pytest; Go with a module cache; Temurin 21 + javac; gcc/make; the Postgres client and server).
Built by `deploy/remote-deploy.sh` and a make target, pinned by digest after the build, added to
schema's `BASE_IMAGES` allowlist, and used by worlds and tasks `FROM`.

**Why it waits:** Docker's layer cache already reuses identical install steps on a machine, so
local builds are about 1.5 min (P1). The gain is mostly on fresh AWS runners with an empty cache.
It's an optimization, not a missing piece.

**When it matters:** if the load test (docs/LOAD_TEST.md) shows image builds as a bottleneck, or
when a batch runs on newly started runners. Owner when picked up: the infrastructure session.

## Batch report generator

**What:** `scripts/batch_report.py <batch_id>` writes a markdown report from the API/DB:
- the batch metrics: how many pass structural validation, how many are functionally
  correct (oracle/nop), how many land in 1-3/5, total cost (authoring vs evaluation) and time,
  and the yield rate;
- per task: family/mechanism, each version (origin, mutation operators + rule + evidence),
  its world sha, the gates, the pass pattern, and the verdict;
- for each accepted task, why the model fails, quoting the Trace Parser's trace evidence;
- the world hardness results (warn mode) against the pass rate.

**Why it waits:** it's needed only to write up a finished batch, not to run one. The API's
`GET /v1/batches/{id}/report` and the verdict files already hold the data.

**When it matters:** before publishing a batch's results. Owner when picked up: the api-service
session (-75).

## Mutation-testing gate (test strength)

**What:** not the same as *world mutation* (EX1, which makes a too-easy task harder). This
measures how strong a task's hidden tests are. During validate (and in `sde-task`'s
self-check), generate 10-20 small automatic bugs ("mutants") in the reference-solved repo,
limited to the files the solution changes: flip a comparison, off-by-one, drop a statement or
write, remove a constraint or index, swap a call's arguments, return early. Run the hidden
tests against each, in parallel on the already-built image, and require a kill rate of at least
70% (`SDE_MIN_MUTANT_KILL`). Surviving mutants go back to the task author as repair feedback
("these changes to the fixed code still pass your tests: …"). Skip mutants that don't parse or
don't change behavior; time-box it to about 5 min per bundle.

**What exists today:** two hand-picked broken versions, the nop gate (the untouched repo must
fail) and the wrong-fix gate (the world's plausible wrong fix must fail).

**Why it waits:** useful (weak tests let partial or hacky fixes pass, the reward-hacking concern, and it makes "too easy" verdicts more trustworthy), but not urgent. The semantic leak
check and the invariant property tests come first.

**When it matters:** if batch results show passes that a review judges partial or hacky, or
before shipping the final batch. Owner when picked up: the api-service session (-75).

## Hardness tiebreak in `choose_mutations`

**What:** when a task comes back too easy, `calibration.choose_mutations` picks up to 2
operators from the pass-review evidence. The tiebreak (the user chose this version over
ranking hardness first) lets the world_check hardness result fill only a slot that would
otherwise go to a generic fallback: a local fix with no dependent → `extend_cross_component_dependency`
(rule `hardness_local_fix`); a trap that isn't tempting → a "strengthen plausible_wrong_fix"
note (rule `hardness_trap`). The Trace Parser copies `result.hardness` from the version's
world_check row into the mutation job's `input.hardness`; world_mutation.md (9e730fd) already
renders it.

**Why it waits:** coding was stopped for the docs pass before it was merged. It's parked on a
branch by the executor-worker session (-c0). Without it, `input.hardness` is absent and the
mutation prompt's hardness section stays empty; nothing else changes.

**When it matters:** once the batch shows whether the warn-mode hardness results correlate with
lower pass rates. Owner when picked up: -c0.

## A request timeout for the solver's model calls

**What:** P1's rollout 2 hung on one model call for 11+ minutes after the agent said it was
done, and would have waited until the 60-minute agent timeout. Set a per-request timeout (and
retry) for terminus-2's LiteLLM calls in the Executor's Harbor invocation, e.g. 300 s.

**Why it waits:** found during the run, after coding stopped. One occurrence so far.

**When it matters:** for any batch, since a hung call wastes a slot for up to an hour. Owner
when picked up: -c0 (evaluation/harbor.py).
