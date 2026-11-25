"""The pinned evaluation config and the band rules (docs/PRD.md §6.6). The API, the hand-offs and
the Trace Parser all read them, so there is one definition of what a band measurement means."""

import hashlib
import json
from typing import Any

from shared.contracts import config


def eval_config() -> dict[str, Any]:
    """The pinned evaluation config every scored rollout of a version must share."""
    return {
        "agent": config.EVAL_AGENT,
        "model": config.EVAL_MODEL,
        "agent_kwargs": {"reasoning_effort": config.EVAL_REASONING_EFFORT},
        "n_runs": config.N_RUNS,
        "band": [config.BAND_LOW, config.BAND_HIGH],
    }


def config_hash() -> str:
    return hashlib.sha256(json.dumps(eval_config(), sort_keys=True).encode()).hexdigest()[:16]


def in_band(pass_count: int) -> bool:
    return config.BAND_LOW <= pass_count <= config.BAND_HIGH


def too_easy(pass_count: int) -> bool:
    return pass_count > config.BAND_HIGH


def too_hard(pass_count: int) -> bool:
    return pass_count < config.BAND_LOW
