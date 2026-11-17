"""
Executor settings. The pinned evaluation config (agent, model, reasoning
effort, runs, band) lives in shared/contracts/config.py and is imported from there,
so there is exactly one place that defines what a band measurement means.
Where traces and bundles live is the artifact store's business
(shared/storage, SDE_WORK_DIR).
"""

import os
from pathlib import Path

from shared.contracts import config

ROOT_DIR = Path(__file__).resolve().parent.parent

EVAL_AGENT = config.EVAL_AGENT
EVAL_MODEL = config.EVAL_MODEL
EVAL_REASONING_EFFORT = config.EVAL_REASONING_EFFORT
N_RUNS = config.N_RUNS
BAND_LOW = config.BAND_LOW
BAND_HIGH = config.BAND_HIGH

# Passed to `harbor run --env-file` when present, so LiteLLM inside Harbor sees
# OPENROUTER_API_KEY / OPENROUTER_API_BASE.
ENV_FILE = Path(os.environ.get("SDE_ENV_FILE", ROOT_DIR / ".env"))

HARBOR_BIN = os.environ.get("HARBOR_BIN", "harbor")

# Leases, heartbeats, polling, Docker slots and retry/attempt limits are shared
# by every pipeline component and live in shared/contracts/models.py.

# An INVALID rollout's retry waits RETRY_BACKOFF_SEC * 2**retry.
RETRY_BACKOFF_SEC = float(os.environ.get("SDE_RETRY_BACKOFF_SEC", "60"))

# Circuit breaker: if this many rollouts came back INVALID within the window,
# the gateway or Docker is probably down, so stop claiming for a while instead
# of burning every slot's retries.
BREAKER_INVALID = int(os.environ.get("SDE_BREAKER_INVALID", "5"))
BREAKER_WINDOW_SEC = float(os.environ.get("SDE_BREAKER_WINDOW_SEC", "600"))
BREAKER_PAUSE_SEC = float(os.environ.get("SDE_BREAKER_PAUSE_SEC", "300"))

# How long a cancelled Harbor run gets to clean up its containers after SIGTERM.
CANCEL_GRACE_SEC = float(os.environ.get("SDE_CANCEL_GRACE_SEC", "30"))
