"""Tests for trial classification. Run with: python -m pytest evaluation"""

import copy

import pytest

from evaluation.classify import INVALID, VALID_FAIL, VALID_PASS, classify

EXPECTED = {"agent": "terminus-2", "model": "openrouter/z-ai/glm-5.3-flash",
            "kwargs": {"reasoning_effort": "high"}}

# Shaped like a Harbor 0.26.0 trial result.json.
TRIAL = {
    "trial_name": "fix-thing__abc123",
    "task_checksum": "c0ffee",
    "started_at": "2026-11-17T22:04:03.354617Z",
    "finished_at": "2026-11-17T22:09:03.354617Z",
    "config": {"agent": {"name": "terminus-2", "model_name": "openrouter/z-ai/glm-5.3-flash",
                         "kwargs": {"reasoning_effort": "high"}}},
    "agent_result": {"n_input_tokens": 1200, "n_output_tokens": 300, "cost_usd": 0.0021},
    "verifier_result": {"rewards": {"reward": 1.0}},
    "exception_info": None,
}


def trial(**changes) -> dict:
    t = copy.deepcopy(TRIAL)
    t.update(changes)
    return t


def test_reward_one_is_a_valid_pass():
    m = classify(trial(), EXPECTED)
    assert (m.outcome, m.reward, m.error) == (VALID_PASS, 1.0, None)
    assert m.cost_usd == 0.0021 and m.duration_sec == 300.0 and m.task_checksum == "c0ffee"


def test_reward_zero_is_a_valid_fail():
    m = classify(trial(verifier_result={"rewards": {"reward": 0.0}}), EXPECTED)
    assert (m.outcome, m.reward) == (VALID_FAIL, 0.0)


@pytest.mark.parametrize("exc", ["AgentTimeoutError", "VerifierTimeoutError", "EnvironmentStartTimeoutError",
                                 "RewardFileNotFoundError", "RateLimitError"])
def test_any_exception_is_invalid_even_with_a_reward(exc):
    t = trial(verifier_result={"rewards": {"reward": 0.0}},
              exception_info={"exception_type": exc, "exception_message": "boom\nmore detail",
                              "exception_traceback": "", "occurred_at": "2026-11-17T22:05:00Z"})
    m = classify(t, EXPECTED)
    assert m.outcome == INVALID and m.reward is None
    assert m.error == f"{exc}: boom"
    assert m.raw_reward == 0.0  # kept for the audit, never scored


CLOSED = ("x__env: failed to send non-blocking keys: command=\"tmux send-keys -t terminus-2 -- 'ls\\n'\", "
          "return_code=1, stderr=None, stdout='no server running on /tmp/tmux-1000/default\\n'")


@pytest.mark.parametrize("reward,outcome", [(1.0, VALID_PASS), (0.0, VALID_FAIL)])
def test_the_agent_closing_its_own_terminal_is_measured_by_the_verifier(reward, outcome):
    # An `exit 1` (or set -e) at the prompt logged the shell out; the verifier still ran.
    t = trial(verifier_result={"rewards": {"reward": reward}},
              exception_info={"exception_type": "RuntimeError", "exception_message": CLOSED})
    m = classify(t, EXPECTED)
    assert (m.outcome, m.reward, m.error) == (outcome, reward, None)


def test_a_closed_terminal_without_a_verifier_result_is_still_invalid():
    t = trial(verifier_result=None, exception_info={"exception_type": "RuntimeError", "exception_message": CLOSED})
    assert classify(t, EXPECTED).outcome == INVALID


def test_other_send_keys_failures_stay_invalid():
    # A refused send (e.g. a NUL byte) is the harness's problem, not a closed terminal.
    t = trial(exception_info={"exception_type": "ValueError", "exception_message": "embedded null byte"})
    assert classify(t, EXPECTED).outcome == INVALID
    t = trial(exception_info={"exception_type": "RuntimeError",
                              "exception_message": "failed to send non-blocking keys: return_code=1, stdout=''"})
    assert classify(t, EXPECTED).outcome == INVALID


def test_step_exception_is_invalid():
    t = trial(step_results=[{"step_name": "s1", "exception_info": {"exception_type": "AgentTimeoutError",
                                                                    "exception_message": ""}}])
    assert classify(t, EXPECTED).error == "AgentTimeoutError"


def test_missing_verifier_result_is_invalid():
    m = classify(trial(verifier_result=None), EXPECTED)
    assert (m.outcome, m.error) == (INVALID, "NoVerifierResult")


def test_non_binary_reward_is_invalid():
    m = classify(trial(verifier_result={"rewards": {"reward": 0.5}}), EXPECTED)
    assert m.outcome == INVALID and m.error.startswith("NonBinaryReward")


@pytest.mark.parametrize("agent, field", [
    ({"name": "terminus-2", "model_name": "openrouter/z-ai/glm-5.1", "kwargs": {"reasoning_effort": "high"}}, "model"),
    ({"name": "terminus-2", "model_name": "openrouter/z-ai/glm-5.3-flash", "kwargs": {"reasoning_effort": "low"}}, "kwargs"),
    ({"name": "oracle", "model_name": "openrouter/z-ai/glm-5.3-flash", "kwargs": {"reasoning_effort": "high"}}, "agent"),
])
def test_a_trial_run_under_another_config_does_not_count(agent, field):
    m = classify(trial(config={"agent": agent}), EXPECTED)
    assert m.outcome == INVALID and m.error.startswith(f"ConfigMismatch: {field}")
