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
from shared.storage.sql import Conn, job_id, rollout_job_id
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
        runs = [j for j in jobs if j["type"] == JobType.ROLLOUT and j["version"] == n]
        scored, retries, waiting, exhausted = self._slots(tid, n, runs)
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
        summary = (f"{passes}/{self.n_runs} passed" + (f"; task blamed by {blamed_task}" if blamed_task else "")
                   + {"accept": "; accepted", "reject": f"; rejected ({reason})",
                      "revise": f"; revising with {operator}"}[action])
        fields = _result(action, summary, pass_count=passes, operator=operator)
        fields["cost_usd"] = round(cost, 6)

        def handoff(conn: Conn, row: dict[str, Any]) -> list[dict[str, Any]]:
            followups: list[dict[str, Any]] = []
            if action == "accept":
                ok = tasks.accept(conn, tid, n)
            elif action == "reject":
                ok = tasks.reject(conn, tid, reason, Stage.OUT_OF_BAND if reason == RejectReason.EXHAUSTED
                                  else Stage.INVALID, version=n, stages=(Stage.EVALUATING,))
            else:
                sha = next(iter(scored.values()))["bundle_sha256"]
                ok = tasks.cas(conn, tid, version=n, stages=(Stage.EVALUATING,),
                               current_version=n + 1, stage=Stage.QUEUED, bundle_sha256=None)
                followups.append({
                    "id": job_id(tid, n + 1, "revise"), "type": JobType.REVISE, "task_id": tid,
                    "version": n + 1, "bundle_sha256": sha,
                    "input": {"origin": "revision", "bundle_sha256": sha, "operator": operator,
                              "result": f"{passes} of {self.n_runs} runs passed.",
                              "evidence": calibration.evidence(enriched)},
                })
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
            if last is not None and last["status"] in (JobStatus.READY, JobStatus.LEASED):
                waiting = True
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

    def _parse(self, run: dict[str, Any]) -> tuple[dict | None, dict | None, float]:
        """trace_parser.parse over one scored run's trace, fetched from the artifact store."""
        rid, art = run["id"], self.backend.artifacts
        art.fetch_trace(rid)
        trace = art.trace_dir(rid)
        trial = (run.get("result") or {}).get("trial")
        trial_dir = trace / trial if trial else next((p.parent for p in trace.glob("*/result.json")), None)
        if trial_dir is None or not (trial_dir / "result.json").exists():
            log.warning("%s: no trace to parse", rid)
            failure = ({"failing_tests": [], "evidence": [], "explanation": None, "attribution": "unexplained",
                        "pitfall_matched": None, "cited_evidence": [], "review_error": "trace not found"}
                       if run["outcome"] == VALID_FAIL else None)
            return None, failure, 0.0
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


def _result(action: str, summary: str, **extra: Any) -> dict[str, Any]:
    return {"outcome": Outcome.OK, "result": {"action": action, "summary": summary,
                                              **{k: v for k, v in extra.items() if v is not None}}}


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
