"""
The API service (docs/PRD.md §4): FastAPI over the `pipeline` store.

Task creation (api/submit.py) returns 202 as soon as the task and its
`create` job are committed; nothing here waits on generation or evaluation.
POST /v1/tasks creates one task; POST /v1/batches creates N under a shared
`batch_id` label, whose report gives the batch's yield. /v1/report covers
whichever tasks the caller names (or created since a time).
"""

import hmac
import os
from typing import Literal

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from shared.storage import budget
from shared.storage import Backend, describe, from_env
from api.submit import BatchConflict, TaskConflict, create_batch, create_task
from shared.taxonomy.taxonomy import families_listing

from . import service

# Which tasks a report or listing covers (api/service.py scope_of): prod, dev or all.
Scope = Literal["prod", "dev", "all"]
from .models import (BatchCreated, BatchRequest, BatchSummary, CancelRequest, TaskCreated, TaskRequest,
                     TaskSummary)


def create_app(backend: Backend | None = None, api_key: str | None = None) -> FastAPI:
    backend = backend or from_env()
    # Operator key checked against X-Api-Key. Empty means /v1 is closed (503), not open.
    api_key = os.environ.get("SDE_API_KEY", "") if api_key is None else api_key
    store = backend.store

    app = FastAPI(title="synthetic-data-engine task pipeline", version="0.2.0")

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if not api_key:
            raise HTTPException(503, "server has no SDE_API_KEY configured")
        if x_api_key is None or not hmac.compare_digest(x_api_key, api_key):
            raise HTTPException(401, "missing or invalid X-Api-Key")

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

    @v1.post("/tasks", status_code=202, response_model=TaskCreated)
    def post_task(body: TaskRequest):
        spec = body.spec.model_dump(exclude_none=True) if body.spec else None
        task_id, created = create_task(backend, prompt=body.prompt, spec=spec, task_id=body.task_id,
                                       raw_prompt=body.raw_prompt, **body.filter_kwargs())
        return {"task_id": task_id, "created": created}

    @v1.get("/families")
    def list_families():
        """The problem families a request's `area` may name, with each one's mechanisms,
        environments, verification methods and shapes."""
        return families_listing()

    @v1.get("/tasks", response_model=list[TaskSummary])
    def list_tasks(status: str | None = None, since: str | None = None, scope: Scope = "all"):
        return service.list_tasks(store, status=status, since=since, scope=scope)

    @v1.get("/report")
    def get_report(task_id: list[str] | None = Query(None), since: str | None = None,
                   scope: Scope | None = None, include_dev: bool = False):
        """Yield, cost and time over the named tasks, or those created at/after `since`, in
        `scope`: prod (the default), dev (mock runs, SDE_DEV_BATCHES) or all.
        include_dev=true (older) means all."""
        return service.report(store, task_ids=task_id, since=since,
                              scope=service.scope_of(scope, include_dev))

    @v1.get("/tasks/{task_id}")
    def get_task(task_id: str):
        return service.get_task(store, task_id)

    @v1.get("/queue")
    def get_queue():
        """Job counts by type and status, and the jobs running now."""
        return service.queue(store)

    @v1.get("/artifacts/{kind}/{key}")
    def list_artifact(kind: str, key: str):
        """The files of a bundle or world (by sha256) or of a job's trace (by job id)."""
        return service.artifact_files(backend.artifacts, kind, key)

    @v1.get("/artifacts/{kind}/{key}/file")
    def read_artifact(kind: str, key: str, path: str):
        """One file of an artifact, as text (the first 1 MB)."""
        return service.artifact_file(backend.artifacts, kind, key, path)

    @v1.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, body: CancelRequest | None = None):
        """Stop a task: no further job of it is claimed and nothing it settles is handed
        off. Idempotent; a decided task is left as it is."""
        body = body or CancelRequest()
        return service.cancel_task(store, task_id, by=body.by, note=body.note)

    @v1.post("/batches/{batch_id}/cancel")
    def cancel_batch(batch_id: str, body: CancelRequest | None = None):
        """Stop every undecided task of a batch (one transaction). Idempotent."""
        body = body or CancelRequest()
        return service.cancel_batch(store, batch_id, by=body.by, note=body.note)

    @v1.post("/batches", status_code=202, response_model=BatchCreated)
    def post_batch(body: BatchRequest):
        spec = body.spec.model_dump(exclude_none=True) if body.spec else None
        batch_id, task_ids, created = create_batch(backend, body.count, batch_id=body.batch_id,
                                                   prompt=body.prompt, spec=spec, budget_usd=body.budget_usd,
                                                   **body.filter_kwargs())
        return {"batch_id": batch_id, "task_ids": task_ids, "created": created}

    @v1.get("/batches", response_model=list[BatchSummary])
    def list_batches():
        return service.list_batches(store)

    @v1.get("/batches/{batch_id}")
    def get_batch(batch_id: str, scope: Scope = "all"):
        return service.get_batch(store, batch_id, scope=scope)

    @v1.get("/batches/{batch_id}/report")
    def get_batch_report(batch_id: str, scope: Scope | None = None, include_dev: bool = False):
        """summary.json over the batch's tasks in `scope` (prod by default), with its yield block."""
        return service.batch_report(store, batch_id, scope=service.scope_of(scope, include_dev))

    app.include_router(v1)

    @app.get("/healthz")
    def healthz():
        try:
            store.conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchall()
        except Exception as e:  # sqlite3 or psycopg error
            return JSONResponse({"status": "error", "db": type(e).__name__}, status_code=503)
        spend = budget.status(store.conn)  # SDE_BUDGET_USD: when exhausted no paid job is claimed
        return {"status": "budget_exhausted" if spend["budget_exhausted"] else "ok", "db": "ok",
                "budget": spend, "backends": describe(backend)}

    @app.exception_handler(service.NotFound)
    def not_found(_: Request, exc: service.NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(TaskConflict)
    @app.exception_handler(BatchConflict)
    def conflict(_: Request, exc: TaskConflict | BatchConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(ValueError)
    def invalid(_: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    return app
