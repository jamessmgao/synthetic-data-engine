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

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from shared.storage import Backend, describe, from_env
from api.submit import BatchConflict, TaskConflict, create_batch, create_task

from . import service
from .models import BatchCreated, BatchRequest, BatchSummary, TaskCreated, TaskRequest, TaskSummary


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
                                       raw_prompt=body.raw_prompt)
        return {"task_id": task_id, "created": created}

    @v1.get("/tasks", response_model=list[TaskSummary])
    def list_tasks(status: str | None = None, since: str | None = None):
        return service.list_tasks(store, status=status, since=since)

    @v1.get("/report")
    def get_report(task_id: list[str] | None = Query(None), since: str | None = None):
        """Yield, cost and time over the named tasks, or those created at/after `since`."""
        return service.report(store, task_ids=task_id, since=since)

    @v1.get("/tasks/{task_id}")
    def get_task(task_id: str):
        return service.get_task(store, task_id)

    @v1.post("/batches", status_code=202, response_model=BatchCreated)
    def post_batch(body: BatchRequest):
        spec = body.spec.model_dump(exclude_none=True) if body.spec else None
        batch_id, task_ids, created = create_batch(backend, body.count, batch_id=body.batch_id,
                                                   prompt=body.prompt, spec=spec)
        return {"batch_id": batch_id, "task_ids": task_ids, "created": created}

    @v1.get("/batches", response_model=list[BatchSummary])
    def list_batches():
        return service.list_batches(store)

    @v1.get("/batches/{batch_id}")
    def get_batch(batch_id: str):
        return service.get_batch(store, batch_id)

    @v1.get("/batches/{batch_id}/report")
    def get_batch_report(batch_id: str):
        """summary.json over the batch, with its yield block."""
        return service.batch_report(store, batch_id)

    app.include_router(v1)

    @app.get("/healthz")
    def healthz():
        try:
            store.conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchall()
        except Exception as e:  # sqlite3 or psycopg error
            return JSONResponse({"status": "error", "db": type(e).__name__}, status_code=503)
        return {"status": "ok", "db": "ok", "backends": describe(backend)}

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
