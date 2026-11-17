"""
Turn one Harbor trial ``result.json`` into a measurement (docs/PRD.md §6.4).

A rollout only counts toward a task's difficulty if it is a clean measurement
of the model: the agent ran to completion under the pinned configuration and
the verifier wrote a binary reward. Everything else is INVALID, which means
"retry this slot", never "the model failed":

    INVALID     Harbor recorded an exception (agent/verifier/environment
                timeouts, API or sandbox errors, missing or empty reward
                file, ...), there is no verifier result, the reward is not
                exactly 0 or 1, or the trial ran under a different agent,
                model or reasoning effort than the job asked for.
    VALID_PASS  no exception and reward == 1.
    VALID_FAIL  no exception and reward == 0.

Field names follow Harbor 0.26.0's ``TrialResult`` (harbor/models/trial/result.py).
"""

from dataclasses import dataclass
from datetime import datetime

VALID_PASS = "VALID_PASS"
VALID_FAIL = "VALID_FAIL"
INVALID = "INVALID"


@dataclass(frozen=True)
class Measurement:
    outcome: str
    reward: float | None  # None unless the outcome is valid
    error: str | None  # why the trial is INVALID
    raw_reward: float | None  # what the verifier wrote, even for an INVALID trial
    task_checksum: str | None
    cost_usd: float | None
    n_input_tokens: int | None
    n_output_tokens: int | None
    duration_sec: float | None


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _duration(result: dict) -> float | None:
    start, end = _parse_time(result.get("started_at")), _parse_time(result.get("finished_at"))
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _config_mismatch(result: dict, expected: dict) -> str | None:
    """Compare the agent config Harbor actually ran with the one the job pinned."""
    agent = (result.get("config") or {}).get("agent") or {}
    ran = {
        "agent": agent.get("name"),
        "model": agent.get("model_name"),
        "kwargs": agent.get("kwargs") or {},
    }
    for key in ("agent", "model", "kwargs"):
        if key in expected and ran[key] != expected[key]:
            return f"ConfigMismatch: {key} was {ran[key]!r}, expected {expected[key]!r}"
    return None


def _exception(result: dict) -> str | None:
    info = result.get("exception_info")
    if not info:
        # Multi-step tasks record exceptions per step.
        for step in result.get("step_results") or []:
            if step.get("exception_info"):
                info = step["exception_info"]
                break
    if not info:
        return None
    message = (info.get("exception_message") or "").strip().splitlines()
    first = f": {message[0][:200]}" if message else ""
    return f"{info.get('exception_type', 'Exception')}{first}"


def classify(result: dict, expected: dict) -> Measurement:
    """Classify one trial. ``expected`` holds the pinned ``agent``, ``model`` and ``kwargs``."""
    agent_result = result.get("agent_result") or {}
    rewards = (result.get("verifier_result") or {}).get("rewards") or {}
    raw_reward = rewards.get("reward")

    error = _exception(result) or _config_mismatch(result, expected)
    if error is None and raw_reward is None:
        error = "NoVerifierResult"
    if error is None and raw_reward not in (0, 1):
        error = f"NonBinaryReward: {raw_reward!r}"

    if error is not None:
        outcome, reward = INVALID, None
    else:
        reward = float(raw_reward)
        outcome = VALID_PASS if reward == 1 else VALID_FAIL

    return Measurement(
        outcome=outcome,
        reward=reward,
        error=error,
        raw_reward=raw_reward,
        task_checksum=result.get("task_checksum"),
        cost_usd=agent_result.get("cost_usd"),
        n_input_tokens=agent_result.get("n_input_tokens"),
        n_output_tokens=agent_result.get("n_output_tokens"),
        duration_sec=_duration(result),
    )
