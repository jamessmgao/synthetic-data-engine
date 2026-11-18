"""
The Trace Parser service: analysis + revision (docs/PRD.md §5.1, §6.5, §6.6).

Every finished run pings the analyze queue (`<rollout id>-analyze`, from the
executor's settle, or from the reconciler when a run is dead-lettered). For the
ping's (task, version), this service:

1. finishes with `stale` if the task was decided or moved to another version;
2. looks at the version's five run slots. A slot whose last attempt is DEAD or
   INVALID with retries left gets its retry (the executor already enqueued one
   for INVALID; this covers DEAD runs). A slot out of retries rejects the task
   (`infra_unresolved`). Any slot still open: finish with `wait`;
3. once every slot is scored, fetches the five traces (S3 in production),
   extracts metrics and evidence and has GLM-5.1 review each failure
   (evaluation/trace_parser.py), then decides:

       1–3 passes, no failure blamed on the task → ACCEPTED, bundle published
       a failure blamed on the task               → revision `repair_ambiguity`
       out of band, version < MAX_VERSIONS        → revision with an operator
       out of band at MAX_VERSIONS                → REJECTED (exhausted)

   A revision is a `revise` job on the revise queue for version n+1, carrying
   the old bundle, the operator and the review as evidence; the Generation
   Service picks it up.

The slow part (fetching, parsing, the review) runs outside any transaction. The
decision is applied in the analyze job's settle transaction as a compare-and-set
on (current_version, stage EVALUATING), together with the `trace_metrics` and
`failure` it writes onto the run rows. So several pings for one version, several
copies of this service, or a kill at any point still decide each version once.

    python -m evaluation.trace_parser_service [--concurrency 2]
"""

import argparse
import json
import logging
import os
import signal
import socket
import threading
from pathlib import Path
from typing import Any, Callable

from evaluation import calibration, trace_parser, verdict
from evaluation.classify import VALID_FAIL
from shared.jobs import tasks
from shared.storage import Backend, describe, from_env
from shared.storage.sql import Conn, job_id, rollout_job_id, utcnow
from shared.jobs.jobqueue import Handoff, JobQueue
from shared.contracts.models import (
    MAX_ROLLOUT_RETRIES,
    MAX_VERSIONS,
    POLL_S,
    RECONCILE_S,
    SCORED_OUTCOMES,
    TERMINAL_TASK_STATUSES,
    JobStatus,
    JobType,
    Outcome,
    RejectReason,
    Stage,
    TaskStatus,
)
from shared.jobs.reconciler import ACCEPTED_DIR
from shared.jobs.worker import run_one

log = logging.getLogger("trace_parser")

REPAIR_AMBIGUITY = "repair_ambiguity"


class TraceParserService:
    def __init__(self, backend: Backend, accepted_dir: Path | None = None, complete: Callable | None = None):
        self.backend = backend
        self.jq = JobQueue(backend)
        self.accepted_dir = accepted_dir or ACCEPTED_DIR
        self.complete = complete  # the GLM-5.1 call; tests pass a fake
        self.n_runs = calibration.eval_config()["n_runs"]

    # --- one analyze job ------------------------------------------------------------

    def process(self, job: dict[str, Any]) -> bool:
        """Analyze, settle with the decision, publish if accepted. True if settled."""
        decided: dict[str, Handoff | None] = {}

        def fn(j: dict[str, Any]) -> dict[str, Any]:
            fields, decided["handoff"] = self.analyze(j)
            return fields

        def handoff(conn: Conn, row: dict[str, Any]) -> list[dict[str, Any]]:
            h = decided.get("handoff")
            return h(conn, row) if h else []

        settled = run_one(self.jq, job, fn, handoff)
        if settled:
            task = self.backend.store.task(job["task_id"])
            if task and task["status"] == TaskStatus.ACCEPTED:
                try:  # after the commit; publish_pending() retries it if this fails
                    self.publish(task)
                except Exception:
                    log.exception("%s: publishing failed; will retry", task["id"])
        return settled

    def analyze(self, job: dict[str, Any]) -> tuple[dict[str, Any], Handoff | None]:
        tid, n = job["task_id"], job["version"]
        task = self.backend.store.task(tid)
        if (task is None or task["status"] in TERMINAL_TASK_STATUSES or task["current_version"] != n
                or task["stage"] != Stage.EVALUATING):
            return _result("stale", f"task is {task and task['status']} v{task and task['current_version']} "
                                    f"{task and task['stage']}, not v{n} EVALUATING"), None

        jobs = self.backend.store.task_jobs(tid)
        # Timed-out runs first: the Trace Parser's ruling makes each VALID_FAIL or INVALID, and the
        # decision below sees the ruled rows. The rulings are written in this analysis' settle.
        rulings, ruling_cost = self._rule_timeouts(
            [j for j in jobs if j["type"] == JobType.ROLLOUT and j["version"] == n])
        jobs = [{**j, **rulings[j["id"]]} if j["id"] in rulings else j for j in jobs]
        fields, handoff = self._decide(task, jobs)
        if rulings:
            fields["cost_usd"] = round(fields.get("cost_usd", 0.0) + ruling_cost, 6)
            fields["result"]["timeouts_ruled"] = {rid: r["outcome"] for rid, r in rulings.items()}
        return fields, _with_rulings(rulings, handoff)

    def _decide(self, task: dict[str, Any], jobs: list[dict[str, Any]]) -> tuple[dict[str, Any], Handoff | None]:
        tid, n = task["id"], task["current_version"]
        runs = [j for j in jobs if j["type"] == JobType.ROLLOUT and j["version"] == n]
        scored, retries, waiting, exhausted = self._slots(tid, n, runs)
        early = early_stop(scored, self.n_runs) if (waiting or exhausted) else None
        if early:  # the remaining runs can't change the verdict: decide on the scored ones
            waiting, exhausted = False, []
        if exhausted:
            return (_result("reject", f"slots {exhausted} used {MAX_ROLLOUT_RETRIES} retries without a valid run"),
                    _reject(tid, n, RejectReason.INFRA_UNRESOLVED, Stage.UNRESOLVED))
        if waiting:
            return _result("wait", f"{len(scored)}/{self.n_runs} runs scored"), _still_evaluating(tid, n, retries)

        # Every slot scored: parse the traces, review the failures, decide.
        parsed, cost = {}, 0.0
        for slot, run in sorted(scored.items()):
            metrics, failure, c = self._parse(run)
            parsed[run["id"]] = (metrics, failure)
            cost += c
        enriched = [{**run, "trace_metrics": parsed[run["id"]][0], "failure": parsed[run["id"]][1]}
                    for run in scored.values()]
        passes = sum(r["outcome"] == Outcome.VALID_PASS for r in enriched)
        blamed_task = [r["id"] for r in enriched if (r["failure"] or {}).get("attribution") == "task"]

        if calibration.in_band(passes) and not blamed_task:
            action, operator = "accept", None
        elif n >= MAX_VERSIONS:
            action, operator = "reject", None
        elif blamed_task:
            action, operator = "revise", REPAIR_AMBIGUITY
        else:
            used = [(j["input"] or {}).get("operator") for j in jobs if j["type"] == JobType.REVISE]
            action, operator = "revise", calibration.choose_operator(passes, enriched, [o for o in used if o])
        reason = RejectReason.INVALID if blamed_task else RejectReason.EXHAUSTED
        # Too easy: find out exactly why from the passing traces, so the revision
        # (or the rejection record) says what made it easy and how to harden it.
        analysis = None
        if not blamed_task and calibration.too_easy(passes):
            analysis, c = self._review_passes(tid, n, enriched, passes)
            cost += c
        direction = ("repair" if blamed_task else "easier" if calibration.too_hard(passes)
                     else "harder" if calibration.too_easy(passes) else None)
        # World mutation for the next version (plan §3.5): which operators, aimed at which
        # fix. The history travels in the revise inputs, so nothing shared is rewritten.
        mutation, history = None, _mutation_history(jobs)
        solver_runs = (self._solver_runs(enriched) if action == "revise" and direction == "harder"
                       and author_mode() == "agent" and _world_sha256(jobs, n) else None)
        if action == "revise":
            chosen = (calibration.choose_mutations(passes, enriched, analysis, history,
                                                     (task.get("slot") or {}).get("family"))
                      if not blamed_task
                      else {"operators": [], "why": [], "rules": [], "target_fix": None})
            mutation = {"version": n + 1, "from_version": n, "pass_count": passes, "direction": direction,
                        **chosen}
        summary = ((f"{len(scored)} scored of {self.n_runs}, early-stopped ({early['why']}); " if early else "")
                   + f"{passes}/{len(scored) if early else self.n_runs} passed"
                   + (f"; task blamed by {blamed_task}" if blamed_task else "")
                   + {"accept": "; accepted", "reject": f"; rejected ({reason})",
                      "revise": f"; revising ({direction}) with {operator}"
                                + (f", mutating: {', '.join(mutation['operators'])}"
                                   if mutation and mutation["operators"] else "")}[action])
        fields = _result(action, summary, pass_count=passes, operator=operator, direction=direction,
                         easy_analysis=analysis, mutation=mutation,
                         **({"early_stopped": {**early, "scored": len(scored), "of": self.n_runs}} if early else {}))
        fields["cost_usd"] = round(cost, 6)

        def handoff(conn: Conn, row: dict[str, Any]) -> list[dict[str, Any]]:
            followups: list[dict[str, Any]] = []
            if early:  # the version's other runs aren't needed: close them first (accept / reject would
                # mark them DEAD, i.e. invalid runs), DONE + SKIPPED, so neither the reconciler nor a retry
                # brings them back; a running one is stopped by its refused heartbeat. In this transaction:
                # if another analysis decided the version first, they weren't needed either.
                now = utcnow()
                conn.execute(
                    """UPDATE jobs SET status = 'DONE', outcome = ?, finished_at = ?, lease_until = NULL,
                                       handled_at = ?, error = ?
                        WHERE task_id = ? AND version = ? AND type = ? AND status IN ('READY', 'LEASED')""",
                    (str(Outcome.SKIPPED), now, now, f"early-stopped: {early['why']} after {len(scored)} scored",
                     tid, n, str(JobType.ROLLOUT)))
            if action == "accept":
                ok = tasks.accept(conn, tid, n)
            elif action == "reject":
                ok = tasks.reject(conn, tid, reason, Stage.OUT_OF_BAND if reason == RejectReason.EXHAUSTED
                                  else Stage.INVALID, version=n, stages=(Stage.EVALUATING,))
            else:
                sha = next(iter(scored.values()))["bundle_sha256"]
                ok = tasks.cas(conn, tid, version=n, stages=(Stage.EVALUATING,),
                               current_version=n + 1, stage=Stage.QUEUED, bundle_sha256=None)
                followups.append(next_job(
                    task, n, sha, _world_sha256(jobs, n), direction=direction, operator=operator,
                    mutation=mutation, history=history, analysis=analysis,
                    result=(f"{passes} of {len(scored)} scored runs passed (early-stopped)." if early
                            else f"{passes} of {self.n_runs} runs passed."), evidence=calibration.evidence(enriched),
                    solver_runs=solver_runs, hardness=_world_hardness(jobs, n)))
            if not ok:  # another copy decided this version first: record that, not our decision
                conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(
                    {"action": "stale", "summary": f"another analysis decided v{n} first ({summary})"}), row["id"]))
                return []
            for rid, (metrics, failure) in parsed.items():
                conn.execute("UPDATE jobs SET trace_metrics = ?, failure = ? WHERE id = ? AND failure IS NULL",
                             (json.dumps(metrics), json.dumps(failure) if failure else None, rid))
            return followups

        return fields, handoff

    def _slots(self, tid: str, n: int, runs: list[dict[str, Any]]):
        """(scored by slot, retries to enqueue, any slot open, slots out of retries)."""
        scored, retries, waiting, exhausted = {}, [], False, []
        for slot in range(1, self.n_runs + 1):
            tries = sorted((j for j in runs if j["slot"] == slot), key=lambda j: j["retry"])
            hit = next((j for j in tries if j["outcome"] in SCORED_OUTCOMES), None)
            if hit:
                scored[slot] = hit
                continue
            last = tries[-1] if tries else None
            if last is not None and (last["status"] in (JobStatus.READY, JobStatus.LEASED)
                                     or last["outcome"] == Outcome.TIMEOUT):
                waiting = True  # running, or a timeout the Trace Parser hasn't ruled on
            elif last is not None and last["retry"] >= MAX_ROLLOUT_RETRIES:
                exhausted.append(slot)
            else:  # never run, or DEAD / INVALID with retries left and no retry queued
                retry = 0 if last is None else last["retry"] + 1
                retries.append({
                    "id": rollout_job_id(tid, n, slot, retry), "type": JobType.ROLLOUT, "task_id": tid,
                    "version": n, "slot": slot, "retry": retry,
                    **({"bundle_sha256": last["bundle_sha256"], "config_hash": last["config_hash"],
                        "input": last["input"]} if last else {}),
                })
                waiting = True
        return scored, retries, waiting, exhausted

    def _trial_dir(self, run: dict[str, Any]) -> Path | None:
        """The run's Harbor trial directory, fetched from the artifact store."""
        rid, art = run["id"], self.backend.artifacts
        art.fetch_trace(rid)
        trace = art.trace_dir(rid)
        trial = (run.get("result") or {}).get("trial")
        trial_dir = trace / trial if trial else next((p.parent for p in trace.glob("*/result.json")), None)
        return trial_dir if trial_dir is not None and (trial_dir / "result.json").exists() else None

    def _review_passes(self, tid: str, n: int, enriched: list[dict[str, Any]], passes: int) -> tuple[dict, float]:
        """Why was this version too easy? A review of its fastest passing runs."""
        passing = [r for r in enriched if r["outcome"] == Outcome.VALID_PASS]
        passing.sort(key=lambda r: ((r.get("trace_metrics") or {}).get("llm_calls") or 10**6, r["slot"]))
        chosen = [(f"run {r['slot']}", d) for r in passing[:trace_parser.MAX_PASS_RUNS]
                  if (d := self._trial_dir(r)) is not None]
        if not chosen:
            return {"why_easy": None, "trusted": False, "review_error": "no passing trace found"}, 0.0
        sha = passing[0].get("bundle_sha256")
        analysis, cost = trace_parser.review_passes(
            trace_parser.pass_evidence(chosen),
            trace_parser.load_instruction(self.backend.artifacts.bundle_path(sha) if sha else None),
            trace_parser.load_spec(self.backend.work_dir, sha),
            passed=passes, runs=self.n_runs, cache_key=f"pass-review:{tid}-v{n}",
            llm_dir=self.backend.work_dir / "llm", complete=self.complete)
        return {**analysis, "runs_reviewed": [label for label, _ in chosen]}, cost

    def _solver_runs(self, enriched: list[dict[str, Any]]) -> dict[str, str]:
        """{"run<slot>.md": condensed trace} per passing run, for the world-mutation author."""
        out = {}
        for r in sorted(enriched, key=lambda r: r["slot"]):
            if r["outcome"] == Outcome.VALID_PASS and (d := self._trial_dir(r)) is not None:
                if (text := trace_parser.solver_run_summary(f"run {r['slot']}", d)):
                    out[f"run{r['slot']}.md"] = text
        return out

    def _rule_timeouts(self, runs: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], float]:
        """{run id: the ruled fields} for the version's unruled TIMEOUT runs, and the review cost.

        trace_parser.review_timeout decides: the model's own failure → VALID_FAIL (reward 0, the
        review as its failure record, attribution model); anything else, or no trace → INVALID,
        which _slots retries like any invalid run."""
        rulings, cost = {}, 0.0
        for run in runs:
            if run["outcome"] != Outcome.TIMEOUT:
                continue
            trial_dir = self._trial_dir(run)
            if trial_dir is None:
                review = {"verdict": "not_model", "category": None, "conclusion": None, "explanation": None,
                          "cited_evidence": [], "evidence": [], "signals": {}, "review_error": "trace not found"}
            else:
                review, c = trace_parser.review_timeout(
                    trial_dir, json.loads((trial_dir / "result.json").read_text()),
                    cache_key=f"timeout-review:{run['id']}", llm_dir=self.backend.work_dir / "llm",
                    complete=self.complete)
                cost += c
            ruling = {k: review.get(k) for k in ("verdict", "category", "signals", "review_error") if k in review}
            if review["verdict"] == "model":
                failure = {"failing_tests": [t["name"] for t in trace_parser.failing_tests(trial_dir)],
                           "evidence": review["evidence"], "conclusion": review["conclusion"],
                           "explanation": review["explanation"],
                           "explanation_cut": trace_parser.is_cut(review["explanation"]),
                           "attribution": "model", "pitfall_matched": None,
                           "cited_evidence": review["cited_evidence"], "timeout_review": ruling}
                rulings[run["id"]] = {"outcome": Outcome.VALID_FAIL, "reward": 0.0, "error": run.get("error"),
                                      "failure": failure}
            else:
                why = review.get("conclusion") or review.get("review_error") or "not the model's failure"
                rulings[run["id"]] = {"outcome": Outcome.INVALID, "reward": None,
                                      "error": f"{run.get('error')}; timeout review: {review.get('category')}: {why}",
                                      "failure": {"conclusion": review.get("conclusion"),
                                                  "explanation": review.get("explanation"),
                                                  "cited_evidence": review.get("cited_evidence"),
                                                  "evidence": review.get("evidence"), "timeout_review": ruling}}
        return rulings, cost

    def _parse(self, run: dict[str, Any]) -> tuple[dict | None, dict | None, float]:
        """trace_parser.parse over one scored run's trace, fetched from the artifact store."""
        rid, art = run["id"], self.backend.artifacts
        trial_dir = self._trial_dir(run)
        if trial_dir is None:
            log.warning("%s: no trace to parse", rid)
            failure = ({"failing_tests": [], "evidence": [], "explanation": None, "attribution": "unexplained",
                        "pitfall_matched": None, "cited_evidence": [], "review_error": "trace not found"}
                       if run["outcome"] == VALID_FAIL else None)
            return None, failure, 0.0
        if (run.get("failure") or {}).get("timeout_review"):  # ruled from its timeout: already explained
            return trace_parser.trace_metrics(trial_dir, json.loads((trial_dir / "result.json").read_text())), \
                run["failure"], 0.0
        sha = run.get("bundle_sha256")
        return trace_parser.parse(
            trial_dir, json.loads((trial_dir / "result.json").read_text()), run["outcome"], job_id=rid,
            work_dir=self.backend.work_dir, bundle_sha256=sha,
            bundle_dir=art.bundle_path(sha) if sha else None, complete=self.complete,
        )

    # --- publishing ---------------------------------------------------------------------

    def publish(self, task: dict[str, Any]) -> Path:
        return verdict.publish(self.backend.artifacts, self.accepted_dir, task,
                               self.backend.store.task_jobs(task["id"]))

    def publish_pending(self) -> int:
        """Publish ACCEPTED tasks not yet in output/accepted/ (a crash between the
        ACCEPTED commit and the copy). Returns how many it published."""
        done = 0
        for row in self.backend.store.conn.execute(
                "SELECT id FROM tasks WHERE status = ?", (str(TaskStatus.ACCEPTED),)).fetchall():
            task = self.backend.store.task(row["id"])
            if verdict.published_dir(self.accepted_dir, task) is None:
                self.publish(task)
                done += 1
        return done

    # --- the loop -----------------------------------------------------------------------

    def serve(self, concurrency: int = 2, stop: threading.Event | None = None,
              publish_every_s: float = RECONCILE_S) -> None:
        stop = stop or threading.Event()
        base = f"{socket.gethostname()}:{os.getpid()}:analyze"

        def loop(owner: str) -> None:
            while not stop.is_set():
                job = self.jq.claim(JobType.ANALYZE, owner)
                if job is None:
                    stop.wait(POLL_S)
                    continue
                self.process(job)

        def publisher() -> None:
            while not stop.wait(publish_every_s):
                try:
                    self.publish_pending()
                except Exception:  # a database or S3 blip: try again next pass
                    log.exception("publishing pending accepted tasks failed")

        threads = [threading.Thread(target=loop, args=(f"{base}:{i}",), name=f"analyze-{i}")
                   for i in range(concurrency)]
        threads.append(threading.Thread(target=publisher, name="publisher", daemon=True))
        for t in threads:
            t.start()
        for t in threads[:-1]:
            while t.is_alive():
                t.join(timeout=0.5)


def early_stop(scored: dict[int, dict[str, Any]], n_runs: int) -> dict[str, Any] | None:
    """Is the version's verdict already certain, whatever its unscored slots do? Only scored runs
    count (an INVALID, a timeout or a retry in flight is still "remaining"). With p passes and r
    slots left: p >= the band's low and p + r <= its high → certainly in band; p above the high →
    certainly too easy. Never on too few passes (a later pass could still put it in band)."""
    p = sum(r["outcome"] == Outcome.VALID_PASS for r in scored.values())
    r = n_runs - len(scored)
    if r <= 0:
        return None
    if p >= calibration.config.BAND_LOW and p + r <= calibration.config.BAND_HIGH:
        return {"why": "certain in band", "passes": p, "remaining": r}
    if calibration.too_easy(p):
        return {"why": "certain too easy", "passes": p, "remaining": r}
    return None


def _with_rulings(rulings: dict[str, dict[str, Any]], handoff: Handoff | None) -> Handoff | None:
    """Write the timeout rulings (compare-and-set on outcome TIMEOUT), then the decision's own
    hand-off. If another analysis ruled a run first, this one's decision rests on rulings that
    weren't written: record that and do nothing (the other analysis decides)."""
    if not rulings:
        return handoff

    def apply(conn: Conn, row: dict[str, Any]) -> list[dict[str, Any]]:
        for rid, r in rulings.items():
            if conn.execute("UPDATE jobs SET outcome = ?, reward = ?, error = ?, failure = ? WHERE id = ? AND outcome = ?",
                            (str(r["outcome"]), r["reward"], r["error"], json.dumps(r["failure"]), rid,
                             str(Outcome.TIMEOUT))).rowcount != 1:
                conn.execute("UPDATE jobs SET result = ? WHERE id = ?", (json.dumps(
                    {"action": "stale", "summary": f"another analysis ruled the timed-out run {rid} first"}),
                    row["id"]))
                return []
        return handoff(conn, row) if handoff else []

    return apply


def _result(action: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": Outcome.OK, "result": {"action": action, "summary": summary,
                                              **{k: v for k, v in extra.items() if v is not None}}}


def _mutation_history(jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The world's mutations so far: the latest job that carried a history has the whole list."""
    carried = [j for j in jobs if "mutations" in (j["input"] or {})]
    latest = max(carried, key=lambda j: j["version"], default=None)
    return list(((latest or {}).get("input") or {}).get("mutations") or [])


def _world_sha256(jobs: list[dict[str, Any]], n: int) -> str | None:
    """The world version n's task was written from (agent mode), if any."""
    built = [j for j in jobs if j["version"] == n and (j.get("result") or {}).get("world_sha256")]
    return built[-1]["result"]["world_sha256"] if built else None


def _world_hardness(jobs: list[dict[str, Any]], n: int) -> dict | None:
    """Version n's world hardness record (the last world_check that recorded one), for the
    mutation author: which hardness checks the parent failed or warned on (limit_named …)."""
    checked = [j for j in jobs if j["version"] == n and j["type"] == JobType.WORLD_CHECK
               and (j.get("result") or {}).get("hardness")]
    return checked[-1]["result"]["hardness"] if checked else None


def _world_spec(task: dict[str, Any]) -> dict[str, Any]:
    """The task's spec for a world job; the hand-off copies shape and dial into create, so
    both are always set (a spec v2 has them; an older one gets fix / its difficulty dial)."""
    spec = dict(task.get("slot") or {})
    spec.setdefault("shape", "fix")
    spec.setdefault("dial", (spec.get("difficulty") or {}).get("dial", 2))
    return spec


def author_mode() -> str:
    return os.environ.get("SDE_AUTHOR_MODE", "oneshot")


def next_job(task: dict[str, Any], n: int, sha: str, world_sha: str | None, *, direction: str | None,
             operator: str | None, mutation: dict[str, Any], history: list[dict[str, Any]],
             analysis: dict | None, result: str, evidence: str,
             solver_runs: dict[str, str] | None = None, hardness: dict | None = None) -> dict[str, Any]:
    """The job that produces version n+1 (plan §3.6).

    Agent mode, task written from a world: too easy → a `world` mutation job (change the
    world to defeat what passed); too hard → `revise` (easier, same world); task blamed →
    `revise` (repair_ambiguity). One-shot mode, or a task not written from a world: a
    one-shot `revise` with the operator and the same mutation fields."""
    tid = task["id"]
    mutations = [*history, mutation]
    agent = author_mode() == "agent" and world_sha is not None
    if agent and direction == "harder":
        return {"id": job_id(tid, n + 1, "world"), "type": JobType.WORLD, "task_id": tid, "version": n + 1,
                "bundle_sha256": sha,
                "input": {"origin": "mutation", "parent_world_sha256": world_sha, "spec": _world_spec(task),
                          "mutation": {"operators": mutation["operators"], "why": mutation["why"]},
                          "mutations": mutations, "analysis": analysis, "target_fix": mutation["target_fix"],
                          "result": result, "evidence": evidence,
                          # the world author's only: the task author never sees the solver's traces
                          **({"solver_runs": solver_runs} if solver_runs else {}),
                          **({"hardness": hardness} if hardness else {})}}
    base ={"id": job_id(tid, n + 1, "revise"), "type": JobType.REVISE, "task_id": tid, "version": n + 1,
            "bundle_sha256": sha}
    if agent and direction in ("easier", "repair"):
        return {**base, "input": {"origin": "easier" if direction == "easier" else "repair_ambiguity",
                                  "bundle_sha256": sha, "world_sha256": world_sha, "result": result,
                                  "evidence": evidence, "target_fix": mutation["target_fix"],
                                  "mutations": mutations}}
    return {**base, "input": {"origin": "revision", "bundle_sha256": sha, "operator": operator,
                              "direction": direction, "result": result, "evidence": evidence,
                              "mutation_operators": mutation["operators"], "target_fix": mutation["target_fix"],
                              "mutations": mutations, **({"analysis": analysis} if analysis else {})}}


def _reject(tid: str, n: int, reason: RejectReason, stage: Stage) -> Handoff:
    def handoff(conn: Conn, row: dict[str, Any]) -> list[dict[str, Any]]:
        tasks.reject(conn, tid, reason, stage, version=n, stages=(Stage.EVALUATING,))
        return []

    return handoff


def _still_evaluating(tid: str, n: int, retries: list[dict[str, Any]]) -> Handoff:
    """Enqueue missing retries, but only if the version is still being evaluated."""
    def handoff(conn: Conn, row: dict[str, Any]) -> list[dict[str, Any]]:
        if not retries:
            return []
        t = tasks.task(conn, tid)
        ok = t and t["status"] == TaskStatus.CALIBRATING and t["current_version"] == n \
            and t["stage"] == Stage.EVALUATING
        return retries if ok else []

    return handoff


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Trace Parser service (the analyze queue).")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(threadName)s %(levelname)s %(message)s")
    backend = from_env()
    log.info("backends: %s", describe(backend))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    TraceParserService(backend).serve(args.concurrency, stop)


if __name__ == "__main__":
    main()
