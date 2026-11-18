"""Ride out a database or queue blip instead of crashing the worker.

The RDS resize of 2026-11-18 made Postgres unreachable for ~3 minutes: every worker whose
claim, settle or release hit `psycopg_pool.PoolTimeout` let it escape, the process exited,
and its running jobs were stranded for their whole lease (10-30 min), each re-claim costing
one of MAX_JOB_ATTEMPTS. `retry` repeats a queue call through such errors with backoff.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import TypeVar

log = logging.getLogger("dbretry")
T = TypeVar("T")


def _transient_types() -> tuple[type[BaseException], ...]:
    types: list[type[BaseException]] = [sqlite3.OperationalError, ConnectionError, TimeoutError]
    try:
        import psycopg
        types += [psycopg.OperationalError, psycopg.InterfaceError]
    except ImportError:
        pass
    try:
        import psycopg_pool
        types.append(psycopg_pool.PoolTimeout)
    except ImportError:
        pass
    try:
        import redis.exceptions
        types += [redis.exceptions.ConnectionError, redis.exceptions.TimeoutError]
    except ImportError:
        pass
    return tuple(types)


TRANSIENT = _transient_types()
BACKOFF_START_S = 1.0
BACKOFF_MAX_S = 30.0


def retry(fn: Callable[[], T], what: str, *, budget_s: float | None = None,
          stop: threading.Event | None = None) -> T:
    """Call `fn` until it returns, retrying TRANSIENT errors with exponential backoff (1 s up to
    30 s). Gives up and re-raises once `budget_s` has passed (None: never), or when `stop` is
    set. Any other error is raised at once."""
    start, wait = time.monotonic(), BACKOFF_START_S
    while True:
        try:
            return fn()
        except TRANSIENT as e:
            spent = time.monotonic() - start
            if (budget_s is not None and spent + wait > budget_s) or (stop is not None and stop.is_set()):
                raise
            log.warning("%s: %s after %.0fs, retrying in %.0fs: %s", what, type(e).__name__, spent, wait,
                        str(e)[:200])
            if stop is not None:
                stop.wait(wait)
            else:
                time.sleep(wait)
            wait = min(wait * 2, BACKOFF_MAX_S)
