"""The spend cap: no batch or bug can run away with money.

Two caps, both checked inside the claim (`sql.claim`), so every service (generation,
gates, the Executor, the Trace Parser) obeys them without a loop of its own:

- `SDE_BUDGET_USD`: the whole pipeline (unset or empty = no cap).
- a batch's `budget_usd` (POST /v1/batches), stored in the `batches` table.

A paid job is claimed only while

    recorded spend (SUM jobs.cost_usd) + an estimate for every other leased job
                                      + this job's estimate  <=  cap

for the pipeline, and for the job's batch over that batch's jobs. Leased jobs count
by estimate because their cost is recorded only when they settle (an expired lease
counts too: its worker may have spent before it died). Unpaid jobs (world_check, the
gates in agent mode, whose leak review is reused from the create job) are never
held back, so work already paid for keeps moving. A held-back job stays READY and is
claimed once the cap is raised.

The check is part of the claim's UPDATE, inside the claim's transaction, and every
store transaction is serialized (SQLite BEGIN IMMEDIATE, PostgreSQL
pg_advisory_xact_lock): a claim sees every lease committed before it, so concurrent
workers can't both take the last room under a cap. What can still pass a cap is a
job costing more than its estimate (the cap counts the estimate until it settles).
"""

import logging
import os
import time
from typing import Any

from shared.contracts.models import AUTHOR_MODE, JobType

log = logging.getLogger("budget")

# Estimated USD per job, for jobs not settled yet (refine from the report's per-type costs).
# Agent mode, from P1 (2026-11-17): a world cost $0.51 in 5.4 min, a task $2.39 in 21.6 min;
# about 2x those (create's long tail matters), a revise a bit less than a fresh create.
ESTIMATE_USD: dict[str, float] = (
    {JobType.WORLD: 1.0, JobType.CREATE: 4.0, JobType.REVISE: 3.0, JobType.ROLLOUT: 0.03, JobType.ANALYZE: 0.02}
    if AUTHOR_MODE == "agent" else
    # One-shot drafts are single model calls; validate runs the leak review there.
    {JobType.CREATE: 0.10, JobType.REVISE: 0.10, JobType.VALIDATE: 0.02, JobType.ROLLOUT: 0.03,
     JobType.ANALYZE: 0.02}
)
PAID_JOB_TYPES = tuple(ESTIMATE_USD)
_NO_CAP = 1e18
_WARN_EVERY_S = 60.0
_last_warn = float("-inf")


def cap_usd() -> float | None:
    """SDE_BUDGET_USD in dollars, or None for no cap (read on every claim). Unset, empty, "none",
    "off" or "unlimited" mean no cap (SSM can't store an empty value)."""
    raw = os.environ.get("SDE_BUDGET_USD", "").strip()
    if not raw or raw.lower() in ("none", "off", "unlimited"):
        return None
    cap = float(raw)
    if cap < 0:
        raise ValueError("SDE_BUDGET_USD must be >= 0")
    return cap


def _estimate_sql(col: str) -> str:
    """CASE <col> WHEN 'world' THEN 3.0 ... ELSE 0 END (our own constants, inlined)."""
    whens = " ".join(f"WHEN '{t}' THEN {float(v)!r}" for t, v in ESTIMATE_USD.items())
    return f"(CASE {col} {whens} ELSE 0.0 END)"


def _spend_sql(alias: str, candidate: str) -> str:
    """A job's recorded cost, plus an estimate while it is leased (except the candidate itself)."""
    return (f"COALESCE({alias}.cost_usd, 0) + CASE WHEN {alias}.status = 'LEASED' AND {alias}.id <> {candidate}.id"
            f" THEN {_estimate_sql(f'{alias}.type')} ELSE 0 END")


def claim_filter(candidate: str = "j") -> tuple[str, list[Any]]:
    """The claim's budget condition on the candidate row aliased `candidate`."""
    paid = ",".join(f"'{t}'" for t in PAID_JOB_TYPES)
    est = _estimate_sql(f"{candidate}.type")
    batch_cap = (f"COALESCE((SELECT bb.budget_usd FROM batches bb JOIN tasks bt ON bt.batch_id = bb.id"
                 f" WHERE bt.id = {candidate}.task_id), {_NO_CAP})")
    batch_spend = (f"(SELECT COALESCE(SUM({_spend_sql('bl', candidate)}), 0) FROM jobs bl"
                   f" JOIN tasks blt ON blt.id = bl.task_id"
                   f" WHERE blt.batch_id = (SELECT batch_id FROM tasks WHERE id = {candidate}.task_id))")
    sql = f" AND ({candidate}.type NOT IN ({paid}) OR ({batch_spend} + {est} <= {batch_cap}"
    params: list[Any] = []
    cap = cap_usd()
    if cap is not None:
        sql += f" AND (SELECT COALESCE(SUM({_spend_sql('gl', candidate)}), 0) FROM jobs gl) + {est} <= ?"
        params.append(cap)
    return sql + "))", params


def status(conn, batch_id: str | None = None) -> dict[str, Any]:
    """The pipeline's (or one batch's) cap, spend and whether paid claims have stopped.
    `budget_exhausted` means not even the cheapest paid job fits under the cap."""
    if batch_id is None:
        cap, scope, params = cap_usd(), "", []
    else:
        row = conn.execute("SELECT budget_usd FROM batches WHERE id = ?", (batch_id,)).fetchone()
        cap = row["budget_usd"] if row else None
        scope, params = " WHERE task_id IN (SELECT id FROM tasks WHERE batch_id = ?)", [batch_id]
    row = conn.execute(
        f"""SELECT COALESCE(SUM(cost_usd), 0) AS spent,
                   COALESCE(SUM(CASE WHEN status = 'LEASED' THEN {_estimate_sql('type')} ELSE 0 END), 0) AS leased
              FROM jobs{scope}""", params).fetchone()
    spent, leased = round(float(row["spent"]), 6), round(float(row["leased"]), 6)  # Postgres: numeric
    remaining = None if cap is None else round(cap - spent - leased, 6)
    return {"cap_usd": cap, "spent_usd": spent, "leased_estimate_usd": leased, "remaining_usd": remaining,
            "budget_exhausted": remaining is not None and remaining < min(ESTIMATE_USD.values())}


def warn_if_exhausted(store) -> bool:
    """Log loudly (at most once a minute per process) when the pipeline cap stops paid claims."""
    global _last_warn
    if cap_usd() is None or time.monotonic() - _last_warn < _WARN_EVERY_S:
        return False
    s = status(store.conn)
    if s["budget_exhausted"]:
        _last_warn = time.monotonic()
        log.error("BUDGET EXHAUSTED: $%.2f spent + $%.2f leased of the $%.2f SDE_BUDGET_USD cap; "
                  "no paid job (%s) will be claimed until it is raised",
                  s["spent_usd"], s["leased_estimate_usd"], s["cap_usd"], ", ".join(PAID_JOB_TYPES))
        return True
    return False
