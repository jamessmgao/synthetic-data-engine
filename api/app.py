"""
The API service (docs/PRD.md §4): FastAPI over the service functions.

Submission returns 202 as soon as the tasks and their jobs are committed;
nothing here waits on generation or evaluation.
"""

import hmac
import sqlite3
from collections.abc import Iterator

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from . import service
from .config import Settings
from .db import connect, init_db
from .models import BatchRequest, BatchStatus, BatchSubmitted


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    init_db(settings.db_path)

    app = FastAPI(title="synthetic-data-engine task pipeline", version="0.1.0")

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def require_api_key(x_api_key: str | None = Header(default=None)) -> None:
        if not settings.api_key:
            raise HTTPException(503, "server has no SDE_API_KEY configured")
        if x_api_key is None or not hmac.compare_digest(x_api_key, settings.api_key):
            raise HTTPException(401, "missing or invalid X-Api-Key")

    v1 = APIRouter(prefix="/v1", dependencies=[Depends(require_api_key)])

    @v1.post("/batches", status_code=202, response_model=BatchSubmitted)
    def submit_batch(body: BatchRequest, conn: sqlite3.Connection = Depends(get_conn)):
        return service.submit_batch(conn, body.count, body.batch_id)

    @v1.get("/batches/{batch_id}", response_model=BatchStatus)
    def get_batch(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        return service.get_batch(conn, batch_id, settings)

    @v1.get("/batches/{batch_id}/report")
    def get_report(batch_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        return service.batch_report(conn, batch_id, settings)

    @v1.get("/tasks/{task_id}")
    def get_task(task_id: str, conn: sqlite3.Connection = Depends(get_conn)):
        return service.get_task(conn, task_id, settings)

    app.include_router(v1)

    @app.get("/healthz")
    def healthz(conn: sqlite3.Connection = Depends(get_conn)):
        try:
            conn.execute("SELECT 1 FROM tasks LIMIT 1").fetchall()
        except sqlite3.Error as e:
            return JSONResponse({"status": "error", "db": str(e)}, status_code=503)
        return {"status": "ok", "db": "ok"}

    @app.exception_handler(service.NotFound)
    def not_found(_: Request, exc: service.NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(service.Conflict)
    def conflict(_: Request, exc: service.Conflict):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    return app
