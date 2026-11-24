"""
Difficulty-profiling pilot: many candidates in parallel, measured against the band.

For every (environment x difficulty level) cell it drafts a task, gates it
(repairing up to MAX_REPAIRS times), then runs terminus-2 + glm-5.3-flash up
to 5 times, stopping as soon as the band verdict cannot change:

    4 passes                 -> too_easy   (the 5th run cannot matter)
    >= 1 pass and >= 2 fails -> in_band    (the pass count stays within 1-3)
    5 fails                  -> too_hard

The six examples/ run through the same rollout path as a calibration set.

Parallelism: drafts are API-bound and run DRAFT_CONCURRENCY at once; every
Docker-using step (a validate job, one rollout) holds one of DOCKER_SLOTS.
Spend is checked against BUDGET_USD before every paid step.

Resumable: model calls are cached by job id (work/llm), bundles are content-
addressed, validate steps and rollouts adopt finished Harbor results, and a
candidate with a final verdict in results/<pilot>/ is skipped.

Usage: python experiments/pilot.py [--pilot p1] [--only cell,cell] [--no-examples]
"""

import argparse
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from shared.contracts import config  # noqa: E402
from generator.worker import run_generate_job  # noqa: E402
from validator.gates import run_validate_job  # noqa: E402

BUDGET_USD = 80.0
# Docker slots, split so long agent runs can never starve the gates: a
# rollout may run up to the task's 1-hour agent timeout.
VALIDATE_SLOTS = 1
ROLLOUT_SLOTS = 3
DRAFT_CONCURRENCY = 8
# Examples left out of calibration, with the reason recorded in the results.
SKIPPED_EXAMPLES = {
    "chess-best-move": "not measured: in pilot p1 all four runs were still going after 23+ minutes "
                       "(image-to-FEN task) and held every Docker slot, so it was stopped",
}
N_RUNS = 5
MAX_INVALID_RETRIES = 2  # per rollout slot; timeouts and infra errors are never scored
ROLLOUT_TIMEOUT_SEC = 2 * 3600

ENVIRONMENTS = {
    "pipeline": {"domain": "Data pipelines", "task_type": "Debugging",
                 "core_capability": "Consistency of aggregated results", "failure_mode": "Data corruption",
                 "environment": "Python service", "skill_interaction": "Two dependent skills"},
    "http": {"domain": "Networking", "task_type": "Implementation",
             "core_capability": "Resource management under concurrent requests", "failure_mode": "Timeout",
             "environment": "Python HTTP service", "skill_interaction": "Multiple interacting constraints"},
    "shell": {"domain": "Linux tooling", "task_type": "Investigation",
              "core_capability": "Log and process analysis", "failure_mode": "Resource exhaustion",
              "environment": "Linux container (shell and system tools)", "skill_interaction": "Two dependent skills"},
    "go": {"domain": "Distributed systems", "task_type": "Debugging", "core_capability": "Concurrency",
           "failure_mode": "Race condition", "environment": "Go service", "skill_interaction": "Single skill"},
    "c": {"domain": "Systems programming", "task_type": "Debugging",
          "core_capability": "Memory and resource management", "failure_mode": "Crash",
          "environment": "C program built with make", "skill_interaction": "Two dependent skills"},
    "sqlite": {"domain": "Databases", "task_type": "Migration", "core_capability": "Consistency",
               "failure_mode": "Data corruption", "environment": "Python application with SQLite",
               "skill_interaction": "Multiple interacting constraints"},
    "security": {"domain": "Security", "task_type": "Debugging",
                 "core_capability": "Input validation and access control", "failure_mode": "Authorization bypass",
                 "environment": "Python web application", "skill_interaction": "Two dependent skills"},
    "java": {"domain": "ML infrastructure", "task_type": "Optimization", "core_capability": "Performance",
             "failure_mode": "Resource exhaustion", "environment": "Java application",
             "skill_interaction": "Single skill"},
}

# Concrete lever settings per level (docs/TASK_GENERATION_RULES.md).
LEVELS = {
    "easy": {"level": "easy", "relevant_files": "5-10", "constraints": 1, "subgoals": 1, "failure_scenarios": 1,
             "notes": ["A single, localized defect",
                       "The instruction may name the affected component and show the failing symptom"]},
    "medium": {"level": "medium", "relevant_files": "10-20", "constraints": 2, "subgoals": 2,
               "failure_scenarios": 1,
               "notes": ["Describe symptoms only; the agent must locate the cause itself",
                         "The correct fix touches two modules"]},
    "hard": {"level": "hard", "relevant_files": "15-30", "constraints": 3, "subgoals": 3, "failure_scenarios": 2,
             "notes": ["Describe symptoms only; the agent must locate the cause itself",
                       "At least one constraint interacts with another: the obvious fix for one breaks another",
                       "The defect violates a domain-specific invariant of this system, not a textbook bug pattern",
                       "The relevant evidence (logs, data) must be found among realistic, irrelevant material"]},
}


def grid_specs() -> list[tuple[str, dict]]:
    """Pilot p1: every environment at every level."""
    return [(f"{env}-{level}", {"taxonomy": tax, "difficulty": diff})
            for env, tax in ENVIRONMENTS.items() for level, diff in LEVELS.items()]


# Pilot p2: p1 showed that the level levers add bugs but not difficulty
# (each bug was a one- or two-line fix visible on reading), and that the
# drafted "required guarantees" restated the fix. Each strategy attacks one
# of those causes, at the hard level, in three environments.
STRATEGIES = {
    "s1-interface": [
        "Write the instruction as a user's bug report followed by acceptance criteria.",
        "State every symptom and every guarantee only in terms of the system's external interface: commands, "
        "inputs, outputs, files and exit codes. Never name an internal module, function, class, field or "
        "mechanism, and never describe what the fix must change.",
    ],
    "s2-behavioral": [
        "The defect must be observable only by running the system under specific conditions (operation "
        "ordering, retries, restarts, concurrent access, time zone or locale boundaries, large or unusual "
        "inputs). Reading any single file must not reveal it; the agent has to reproduce it to find it.",
        "State every guarantee only in terms of the system's external interface; never name internal code.",
    ],
    "s3-spec-implementation": [
        "Task type: implementation. A component is missing (a stub that raises 'not implemented'). The agent "
        "must implement it from a precise written specification with several interacting edge cases.",
        "The tests generate many inputs at verify time from a fixed seed and compare the agent's output with a "
        "reference model that lives only in the tests, so hard-coding outputs cannot pass.",
        "Every rule the reference model applies must be stated in the instruction's specification.",
    ],
    "s4-performance": [
        "The existing implementation is correct but far too slow or memory hungry for the stated workload. The "
        "agent must make it meet a resource budget while staying exactly correct.",
        "The tests generate a large input at verify time and enforce a budget with at least a 5x margin "
        "between the reference solution and the original code, so the check is not flaky.",
        "Correctness is checked on the same large input and on small edge cases.",
    ],
}
# s5: p3 showed flash verifies its own work and recovers from its own
# mistakes within the hour (h7: two 40-60 step runs that broke and then fixed
# the code, still passed). What it cannot self-check is a long list of small,
# precisely stated rules: missing one subtle rule still passes its own tests.
# The rule count is a dial for difficulty.
STRATEGIES["s5-many-rules"] = [
    "Task type: implementation. The agent implements one component (a stub that raises 'not implemented') "
    "from a specification of 14 to 18 numbered rules. Each rule is short and precise; several rules "
    "interact (precedence, ordering, normalization, tie-breaking, boundary values, error cases).",
    "Every rule is stated in the instruction exactly once, with a small example where it helps. Nothing the "
    "tests check may be missing from the rules.",
    "The tests generate many inputs at verify time from a fixed seed, designed to exercise every rule and "
    "rule interaction, and compare the output with a reference model that lives only in the tests. Group "
    "the tests by rule so a failure names the rule that was missed.",
]
STRATEGY_ENVIRONMENTS = {
    "s1-interface": ["pipeline", "go", "c"],
    "s2-behavioral": ["http", "sqlite", "go"],
    "s3-spec-implementation": ["pipeline", "c", "shell"],
    "s4-performance": ["pipeline", "java", "go"],
    "s5-many-rules": ["pipeline", "go", "shell"],
}


# The generic hard notes describe a debugging task; these strategies are not one.
STRATEGY_DIFFICULTY = {
    "s3-spec-implementation": {
        **LEVELS["hard"],
        "notes": ["The specification's rules interact, so handling each rule in isolation gives wrong results",
                  "The component plugs into realistic existing code that the agent must read to integrate it"]},
    "s4-performance": {
        **LEVELS["hard"],
        "notes": ["The bottleneck must be found by measuring; it is not obvious from reading one function",
                  "The faster approach must keep the output exactly identical, including ordering and edge cases"]},
    "s5-many-rules": {
        **LEVELS["hard"], "constraints": "14-18 (the numbered rules)", "subgoals": 1,
        "notes": ["Difficulty comes only from the number and interaction of precisely stated rules",
                  "No planted bug and no hidden requirement: a careful engineer who follows every rule passes"]},
}


def strategy_specs() -> list[tuple[str, dict]]:
    specs = []
    for strategy, envs in STRATEGY_ENVIRONMENTS.items():
        for env in envs:
            tax = dict(ENVIRONMENTS[env])
            if strategy in ("s3-spec-implementation", "s5-many-rules"):
                tax["task_type"] = "Implementation"
            elif strategy == "s4-performance":
                tax["task_type"], tax["core_capability"] = "Optimization", "Performance"
            difficulty = STRATEGY_DIFFICULTY.get(strategy, LEVELS["hard"])
            specs.append((f"{strategy}-{env}",
                          {"taxonomy": tax, "difficulty": difficulty, "design": STRATEGIES[strategy]}))
    return specs


# Pilot p3: deliberately hard briefs. In p1/p2, flash solved every task whose
# core was "read the code, spot a local bug" (9/9). The one task it failed
# (configure-git-webserver, 0/5) needed several components to each work, with
# only end-to-end proof. These briefs aim at that: many pieces, no readable
# cause, iterative feedback, exactness under constraints. They avoid trivia,
# unstated requirements, and anything that would fail by timing out.
HARD_COMMON = [
    "A strong engineer should need 1-3 hours. The difficulty must come from the engineering, never from "
    "obscure trivia, unstated requirements, or the work taking longer than an hour of agent time.",
    "State every requirement the tests check in the instruction, in terms of the external interface.",
]
HARD_BRIEFS = {
    "h1-integration": ("shell", [
        "Scenario: in a near-bare container, the agent must bring up a working system of three cooperating "
        "processes (an ingest HTTP endpoint, a background worker, and a query API) that communicate through a "
        "local queue or database, all started by one start script the instruction names. Partial code and "
        "configuration exist for each piece but none of it is wired together.",
        "The system must keep every accepted record when any one process is killed and restarted. The tests "
        "run the start script, drive the system end to end, kill and restart components, and check results.",
        "Everything runs as the unprivileged agent user on high ports; no systemd or root-only tools.",
    ]),
    "h2-reverse-format": ("c", [
        "Scenario: a closed decoder program reads a custom binary record format (versioned header, varint "
        "lengths, per-record checksums, an index at the end). The Dockerfile compiles it from C source, strips "
        "it, and deletes the source. A few sample files with their decoded JSON are provided; there is no "
        "format documentation.",
        "The agent must write an encoder command whose output the decoder turns back into exactly the given "
        "JSON records. The tests generate random records, encode them with the agent's tool, decode them with "
        "the shipped binary, and compare.",
    ]),
    "h3-protocol-server": ("http", [
        "Scenario: implement a TCP key-value server for a small length-prefixed binary protocol that a "
        "provided specification document describes: pipelined requests, requests split across arbitrary packet "
        "boundaries, per-key TTL expiry, and exact error responses.",
        "The tests use a client that fragments writes at random boundaries, opens concurrent connections, and "
        "checks every response byte for byte.",
    ]),
    "h4-go-race": ("go", [
        "Scenario: a Go connection pool or cache passes its existing unit tests but corrupts its state under "
        "concurrent load (leaked or double-handed-out resources, lost updates). Reading any single function "
        "does not reveal the problem.",
        "The tests run a stress harness with the race detector enabled and check invariants, plus the "
        "existing behavior. The Go toolchain and race detector work in the image.",
    ]),
    "h5-layered-build": ("c", [
        "Scenario: a multi-library C project (two static or shared libraries plus a CLI) stopped building "
        "after a directory reorganization. Several independent, layered problems each mask the next: for "
        "example link order, symbol visibility, a header produced by a code generator script, and a runtime "
        "library path.",
        "The tests build from a clean tree with the documented command and then check the CLI's behavior.",
    ]),
    "h6-data-forensics": ("sqlite", [
        "Scenario: an application's SQLite database was damaged (for example overwritten pages or a truncated "
        "file), but a journal or WAL file and the application's own log survive.",
        "The agent must recover every committed record into a clean, consistent database at a path the "
        "instruction names. The ground truth lives only in the tests.",
    ]),
    "h7-exact-performance": ("pipeline", [
        "Scenario: a log reconciliation tool is correct but quadratic. It must process a generated input of "
        "about two million lines within a strict time and memory budget (at least a 5x margin between the "
        "reference solution and the original code) with byte-identical output, including stable tie-breaking "
        "and edge cases.",
        "The tests generate the large input at verify time from a fixed seed, and also check small edge cases.",
    ]),
    "h8-c-extension": ("c", [
        "Scenario: a Python package with a C extension module crashes or silently corrupts results only for "
        "certain inputs (a reference-counting or bounds error). gdb and Python's faulthandler are installed.",
        "The agent must fix the C code without changing the package's Python API. The tests build the "
        "extension and run many generated inputs, including the triggering ones.",
    ]),
}


def hard_brief_specs() -> list[tuple[str, dict]]:
    hard = {**LEVELS["hard"], "notes": []}
    specs = []
    for brief, (env, lines) in HARD_BRIEFS.items():
        tax = dict(ENVIRONMENTS[env])
        if brief == "h8-c-extension":
            tax["environment"] = "Python package with a C extension"
        specs.append((brief, {"taxonomy": tax, "difficulty": hard, "design": lines + HARD_COMMON}))
    return specs


# Pilot p4: briefs built from docs/DIFFICULTY.md. Each raises several
# of B (search), C (constraint coupling), H (horizon), U (unfamiliarity) and
# V (low self-verifiability) at once, with every requirement stated.
FRONTIER_COMMON = HARD_COMMON + [
    "The agent has a shell and an hour; difficulty must survive that. If a quick script or solver would "
    "trivialize the task, raise the coupling between requirements instead of the size.",
    "The agent's own obvious checks should pass for a plausible but wrong solution: the tests probe the "
    "interactions and edge cases the instruction states, not only the main path.",
]
FRONTIER_BRIEFS = {
    "f1-behavior-preserving": ("pipeline", "Debugging", [
        "Scenario: a utility module that many other modules in the repository depend on has a defect or "
        "needs a stated behavior change. The fix must not change any other observable behavior of the "
        "system.",
        "The tests include a large regression suite generated from the original system's behavior across "
        "all the dependent modules (stored only in the tests), plus tests for the stated change.",
    ]),
    "f2-custom-interpreter": ("go", "Implementation", [
        "Scenario: implement the evaluator for a small custom rule or expression language the instruction "
        "fully specifies: scoping, operator precedence, type coercions, short-circuiting and exact error "
        "messages. Its semantics deliberately differ from any common language in a few stated ways.",
        "The tests generate many programs from a fixed seed and compare results with a reference evaluator "
        "that lives only in the tests.",
    ]),
    "f3-optimal-scheduling": ("pipeline", "Implementation", [
        "Scenario: produce optimal schedules for a set of instances (jobs with durations, precedence, shared "
        "resources, time windows). The objective and every constraint are stated. Instances are sized so a "
        "greedy approach is wrong and naive brute force is too slow; a correct dynamic-programming or "
        "branch-and-bound approach finishes quickly.",
        "The tests check feasibility of every schedule and that its cost equals the known optimum stored "
        "only in the tests, including on instances generated at verify time from a fixed seed.",
    ]),
    "f4-ledger-replay": ("sqlite", "Implementation", [
        "Scenario: replay a long stream of financial events into final account balances: postings, "
        "reversals of earlier postings, corrections that arrive out of order, duplicate deliveries that "
        "carry the same idempotency key, multi-currency entries with a stated conversion table, and a "
        "stated rounding rule applied at a stated point.",
        "The tests generate event streams from a fixed seed and compare every balance exactly with a "
        "reference replay that lives only in the tests.",
    ]),
    "f5-incident-diagnosis": ("shell", "Investigation", [
        "Scenario: an incident affected some requests in a system of several services. The agent gets the "
        "services' logs (with clock skew between hosts, interleaved retries, and plenty of unrelated noise) "
        "and their configuration files, but no access to the running system.",
        "The agent must write a report file in a stated format naming the root cause from a stated list of "
        "categories and the exact set of affected request IDs. The tests compare against the ground truth "
        "that lives only in the tests.",
    ]),
    "f6-chained-migration": ("sqlite", "Migration", [
        "Scenario: migrate an application's data and code from schema v1 to v3. v2 exists and the stated "
        "upgrade path goes through it. Each step can break stated invariants (referential integrity, "
        "derived totals, uniqueness under new normalization rules), and the application code must work "
        "against v3.",
        "The tests run the application's stated commands against the migrated database and check every "
        "invariant on data generated from a fixed seed.",
    ]),
}


def frontier_specs() -> list[tuple[str, dict]]:
    specs = []
    for brief, (env, task_type, lines) in FRONTIER_BRIEFS.items():
        tax = {**ENVIRONMENTS[env], "task_type": task_type, "skill_interaction": "Multiple interacting constraints"}
        difficulty = {**LEVELS["hard"], "notes": ["Difficulty comes from coupled decisions that must all be "
                                                  "right, not from size or obscurity"]}
        specs.append((brief, {"taxonomy": tax, "difficulty": difficulty, "design": lines + FRONTIER_COMMON}))
    return specs


SPEC_SETS = {"grid": grid_specs, "strategies": strategy_specs, "hard": hard_brief_specs,
             "frontier": frontier_specs}


class BudgetExceeded(Exception):
    pass


class Pilot:
    def __init__(self, pilot_id: str, validate_slots: int = VALIDATE_SLOTS, rollout_slots: int = ROLLOUT_SLOTS):
        self.id = pilot_id
        self.out = ROOT / "experiments" / "results" / pilot_id
        (self.out / "candidates").mkdir(parents=True, exist_ok=True)
        self.work = Path(config.WORK_DIR)
        self.validate_slots = threading.BoundedSemaphore(validate_slots)
        self.rollout_slots = threading.BoundedSemaphore(rollout_slots)
        self.drafts = threading.BoundedSemaphore(DRAFT_CONCURRENCY)
        self.lock = threading.Lock()
        self.spent = 0.0
        self.started = time.monotonic()

    # --- bookkeeping -------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with self.lock:
            print(line, flush=True)
            with open(self.out / "events.log", "a") as f:
                f.write(line + "\n")

    def charge(self, usd: float) -> None:
        with self.lock:
            self.spent += usd or 0

    def check_budget(self) -> None:
        with self.lock:
            if self.spent >= BUDGET_USD:
                raise BudgetExceeded(f"spent ${self.spent:.2f} of ${BUDGET_USD:.2f}")

    def save(self, cid: str, record: dict) -> None:
        path = self.out / "candidates" / f"{cid}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=1, default=str) + "\n")
        tmp.replace(path)

    def load(self, cid: str) -> dict | None:
        path = self.out / "candidates" / f"{cid}.json"
        return json.loads(path.read_text()) if path.exists() else None

    # --- one candidate -----------------------------------------------------
    def run_candidate(self, cid: str, spec: dict | None, bundle_dir: str | None = None) -> dict:
        prev = self.load(cid)
        if prev and prev.get("verdict"):
            self.log(f"{cid}: already done ({prev['verdict']}), skipping")
            self.charge(prev.get("cost_usd", {}).get("total", 0))
            return prev
        rec: dict = {"id": cid, "spec": spec, "steps": [], "rollouts": [], "verdict": None,
                     "started": time.time()}
        try:
            if bundle_dir is None:
                bundle_dir = self._generate_and_gate(cid, spec, rec)
            else:
                rec["bundle_dir"] = bundle_dir
            if bundle_dir:
                self._evaluate(cid, bundle_dir, rec)
        except BudgetExceeded as e:
            rec["verdict"] = None
            rec["stopped"] = f"budget: {e}"
            self.log(f"{cid}: stopped, {e}")
        except Exception as e:  # keep the other candidates running
            rec["verdict"] = "error"
            rec["error"] = f"{type(e).__name__}: {e}"
            self.log(f"{cid}: ERROR {rec['error']}")
        rec["finished"] = time.time()
        rec["wall_sec"] = round(rec["finished"] - rec["started"], 1)
        gen_cost = sum(s.get("cost_usd", 0) for s in rec["steps"])
        roll_cost = sum(r.get("cost_usd") or 0 for r in rec["rollouts"])
        rec["cost_usd"] = {"authoring": round(gen_cost, 4), "rollouts": round(roll_cost, 4),
                           "total": round(gen_cost + roll_cost, 4)}
        self.save(cid, rec)
        return rec

    def _generate_and_gate(self, cid: str, spec: dict, rec: dict) -> str | None:
        gen_input = {"origin": "initial", "spec": spec}
        for n in range(config.MAX_REPAIRS + 1):
            self.check_budget()
            job_id = f"{self.id}-{cid}-v1-" + ("generate" if n == 0 else f"repair{n}")
            self.log(f"{cid}: drafting ({gen_input['origin']})")
            with self.drafts:
                gen = run_generate_job(job_id, gen_input, self.work)
            self.charge(gen["cost_usd"])
            calls = gen["result"]["llm_calls"]
            rec["steps"].append({"job_id": job_id, "type": "generate", "outcome": gen["outcome"],
                                 "error": gen["error"], "cost_usd": gen["cost_usd"],
                                 "rounds": len(calls),
                                 "completion_tokens": sum(c["completion_tokens"] for c in calls),
                                 "draft_sec": round(sum(c["duration_sec"] for c in calls), 1),
                                 "round_errors": [c["errors"] for c in calls if c["errors"]],
                                 "bundle_sha256": gen["result"].get("bundle_sha256"),
                                 "name": gen["result"].get("name")})
            if gen["outcome"] != "OK":
                rec["verdict"] = "generation_failed"
                self.log(f"{cid}: generation failed: {gen['error'][:200]}")
                return None

            bundle = gen["result"]["bundle_path"]
            val_id = f"{self.id}-{cid}-v1-validate{n}"
            with self.validate_slots:
                t0 = time.monotonic()
                val = run_validate_job(bundle, self.work / "traces" / val_id)
            gates = {k: g["passed"] for k, g in val["result"]["gates"].items()}
            rec["steps"].append({"job_id": val_id, "type": "validate", "outcome": val["outcome"],
                                 "failed_gate": val["failed_gate"], "gates": gates,
                                 "feedback": val["feedback"][:4000], "sec": round(time.monotonic() - t0, 1),
                                 "tests_passing_on_nop": val["result"]["gates"].get("nop", {}).get(
                                     "tests_passing_on_nop")})
            self.log(f"{cid}: gates {gates}")
            if val["outcome"] == "OK":
                rec["bundle_dir"] = bundle
                rec["name"] = gen["result"]["name"]
                return bundle
            gen_input = {"origin": "repair", "bundle_sha256": gen["result"]["bundle_sha256"],
                         "feedback": val["feedback"]}
        rec["verdict"] = "invalid"
        self.log(f"{cid}: invalid after {config.MAX_REPAIRS} repairs")
        return None

    # --- rollouts with early stop -----------------------------------------
    def _evaluate(self, cid: str, bundle_dir: str, rec: dict) -> None:
        state = {"pass": 0, "fail": 0}
        state_lock = threading.Lock()

        def decided() -> str | None:
            if state["pass"] >= 4:
                return "too_easy"
            if state["pass"] >= 1 and state["fail"] >= 2:
                return "in_band"
            if state["fail"] >= N_RUNS:
                return "too_hard"
            return None

        def one_slot(slot: int) -> None:
            for retry in range(MAX_INVALID_RETRIES + 1):
                with state_lock:
                    if decided():
                        return
                self.check_budget()
                with self.rollout_slots:
                    with state_lock:
                        if decided():
                            return
                    r = self._rollout(cid, bundle_dir, f"rollout{slot}-r{retry}")
                self.charge(r.get("cost_usd") or 0)
                r["slot"], r["retry"] = slot, retry
                with state_lock:
                    rec["rollouts"].append(r)
                    if r["outcome"] == "VALID_PASS":
                        state["pass"] += 1
                    elif r["outcome"] == "VALID_FAIL":
                        state["fail"] += 1
                self.log(f"{cid}: rollout{slot} {r['outcome']} ({state['pass']}P/{state['fail']}F)"
                         + (f" [{r['exception']}]" if r["exception"] else ""))
                if r["outcome"] != "INVALID":
                    return

        with ThreadPoolExecutor(N_RUNS) as pool:
            list(pool.map(one_slot, range(1, N_RUNS + 1)))
        rec["passes"], rec["fails"] = state["pass"], state["fail"]
        rec["verdict"] = decided() or "unresolved"
        self.log(f"{cid}: VERDICT {rec['verdict']} ({state['pass']}P/{state['fail']}F)")

    def _rollout(self, cid: str, bundle_dir: str, step: str) -> dict:
        out = self.work / "traces" / f"{self.id}-{cid}"
        job_dir = out / step
        if not list(job_dir.glob("*/result.json")):
            if job_dir.exists():
                job_dir.rename(job_dir.with_name(f"{step}.abandoned-{int(time.time())}"))
            out.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [config.HARBOR_BIN, "run", "-p", bundle_dir, "-a", config.EVAL_AGENT, "-m", config.EVAL_MODEL,
                 "--ak", f"reasoning_effort={config.EVAL_REASONING_EFFORT}", "-k", "1", "-o", str(out),
                 "--job-name", step, "--env-file", str(ROOT / ".env"), "-y", "-q"],
                capture_output=True, text=True, timeout=ROLLOUT_TIMEOUT_SEC,
            )
        return read_trial(job_dir)


def read_trial(job_dir: Path) -> dict:
    trials = sorted(job_dir.glob("*/result.json"))
    if not trials:
        return {"outcome": "INVALID", "exception": "NoTrialResult", "trial_dir": None, "cost_usd": 0}
    trial = trials[0].parent
    r = json.loads(trials[0].read_text())
    exc = (r.get("exception_info") or {}).get("exception_type")
    reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
    ar = r.get("agent_result") or {}
    outcome = ("INVALID" if exc or reward not in (0.0, 1.0)
               else "VALID_PASS" if reward == 1.0 else "VALID_FAIL")
    failing = []
    ctrf = trial / "verifier" / "ctrf.json"
    if ctrf.exists():
        for t in json.loads(ctrf.read_text()).get("results", {}).get("tests", []):
            if t.get("status") != "passed":
                failing.append({"test": t.get("name", "").split("::")[-1],
                                "message": (t.get("message") or "")[:400]})
    steps = None
    traj = trial / "agent" / "trajectory.json"
    if traj.exists():
        try:
            steps = len(json.loads(traj.read_text()).get("steps", []))
        except json.JSONDecodeError:
            pass
    duration = None
    if r.get("started_at") and r.get("finished_at"):
        from datetime import datetime
        duration = round((datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00"))
                          - datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))).total_seconds(), 1)
    return {"outcome": outcome, "reward": reward, "exception": exc,
            "exception_message": ((r.get("exception_info") or {}).get("exception_message") or "")[:300] or None,
            "trial_dir": str(trial), "cost_usd": ar.get("cost_usd"),
            "input_tokens": ar.get("n_input_tokens"), "output_tokens": ar.get("n_output_tokens"),
            "steps": steps, "duration_sec": duration, "failing_tests": failing}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", default="p1")
    parser.add_argument("--only", help="comma-separated candidate ids, e.g. go-hard,ex-fix-git")
    parser.add_argument("--no-examples", action="store_true")
    parser.add_argument("--specs", choices=sorted(SPEC_SETS), default="grid")
    parser.add_argument("--validate-slots", type=int, default=VALIDATE_SLOTS)
    parser.add_argument("--rollout-slots", type=int, default=ROLLOUT_SLOTS)
    args = parser.parse_args()

    pilot = Pilot(args.pilot, args.validate_slots, args.rollout_slots)
    jobs: list[tuple[str, dict | None, str | None]] = []
    if not args.no_examples and (ROOT / "examples").is_dir():  # the calibration set was removed from the repo
        for ex in sorted((ROOT / "examples").iterdir()):
            if (ex / "task.toml").exists() and ex.name not in SKIPPED_EXAMPLES:
                jobs.append((f"ex-{ex.name}", None, str(ex)))
    jobs += [(cid, spec, None) for cid, spec in SPEC_SETS[args.specs]()]
    if args.only:
        wanted = set(args.only.split(","))
        jobs = [j for j in jobs if j[0] in wanted]

    pilot.log(f"pilot {pilot.id}: {len(jobs)} candidates ({args.specs}), budget ${BUDGET_USD}, "
              f"{args.validate_slots}+{args.rollout_slots} docker slots, {DRAFT_CONCURRENCY} concurrent drafts")
    with ThreadPoolExecutor(len(jobs)) as pool:
        results = list(pool.map(lambda j: pilot.run_candidate(*j), jobs))

    summary = {"pilot": pilot.id, "candidates": len(results), "spent_usd": round(pilot.spent, 4),
               "wall_sec": round(time.monotonic() - pilot.started, 1),
               "verdicts": {r["id"]: r.get("verdict") for r in results}}
    (pilot.out / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")
    pilot.log(f"done: spent ${pilot.spent:.2f}; " + json.dumps(summary["verdicts"]))


if __name__ == "__main__":
    main()
