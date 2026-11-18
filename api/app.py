"""
The API service (docs/PRD.md §4): FastAPI over the `pipeline` store.

Submission (api/submit.py) returns 202 as soon as the tasks and their
`create` jobs are committed; nothing here waits on generation or evaluation.
"""

import hmac
import os

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from shared.storage import Backend, describe, from_env
from api.submit import BatchConflict, submit_batch as submit

from . import service
from .models import BatchListItem, BatchRequest, BatchStatus, BatchSubmitted


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

    @v1.post("/batches", status_code=202, response_model=BatchSubmitted)
    def submit_batch(body: BatchRequest):
        batch_id, task_ids, created = submit(backend, body.count, body.batch_id)
        return {"batch_id": batch_id, "task_ids": task_ids, "created": created}

    @v1.get("/batches", response_model=list[BatchListItem])
    def list_batches():
        return service.list_batches(store)

    @v1.get("/batches/{batch_id}", response_model=BatchStatus)
    def get_batch(batch_id: str):
        return service.get_batch(store, batch_id)

    @v1.get("/batches/{batch_id}/report")
    def get_report(batch_id: str):
        return service.batch_report(store, batch_id)

    @v1.get("/tasks/{task_id}")
    def get_task(task_id: str):
        return service.get_task(store, task_id)

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

    @app.exception_handler(BatchConflict)
    def conflict(_: Request, exc: BatchConflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app
