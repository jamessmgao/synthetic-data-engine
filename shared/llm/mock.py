"""
Mock model mode for load tests (docs/LOAD_TEST.md §2): SDE_MOCK_LLM=1 replaces every
model call, and nothing else, with a short sleep and canned output. Docker builds,
test runs, the queue, the database, S3 and the hand-offs all run for real, so a mock
batch measures what the pipeline itself costs.

    ENABLED                 SDE_MOCK_LLM=1
    complete(...)           llm.complete's stand-in: sleeps SDE_MOCK_LLM_S, then returns a
                            schema-valid answer (a registered one, or built from the schema)
    register(name, content) canned content for one response format, by its json_schema name

Mock calls cost 0. The authoring agents' mocks live in authoring/mock_agents.py.
"""

import json
import os
import random
import time
from typing import Any

ENABLED = os.environ.get("SDE_MOCK_LLM") == "1"
LLM_S = float(os.environ.get("SDE_MOCK_LLM_S", "3"))
AUTHOR_STEP_S = float(os.environ.get("SDE_MOCK_AUTHOR_STEP_S", "2"))
AUTHOR_STEPS = int(os.environ.get("SDE_MOCK_AUTHOR_STEPS", "10"))
ROLLOUT_S = float(os.environ.get("SDE_MOCK_ROLLOUT_S", "30"))
PASS_P = float(os.environ.get("SDE_MOCK_PASS_P", "0.4"))
FIXTURE = os.environ.get("SDE_MOCK_FIXTURE") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tests", "fixtures", "p1-local-1")

# Canned answers by response-format name; anything else is built from its schema.
_REGISTERED: dict[str, Any] = {
    "leak_review": {"leaks": [], "verdict": "clean"},
    "blind_judgement": {"match": "none", "why": "mock"},  # the schema's first enum value is a leak
}


def register(name: str, content: Any) -> None:
    _REGISTERED[name] = content


def instance(schema: dict, defs: dict | None = None) -> Any:
    """A minimal value valid for a strict JSON schema: required properties only,
    the first enum value, empty arrays, zeros, "mock" strings."""
    defs = defs if defs is not None else schema.get("$defs", {})
    if "$ref" in schema:
        return instance(defs[schema["$ref"].split("/")[-1]], defs)
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [s for s in schema[key] if s.get("type") != "null"] or schema[key]
            return instance(options[0], defs)
    if "enum" in schema:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object":
        props = schema.get("properties", {})
        return {k: instance(props[k], defs) for k in schema.get("required", list(props))}
    if kind == "array":
        return [instance(schema["items"], defs) for _ in range(schema.get("minItems", 0))]
    return {"string": "mock", "integer": 0, "number": 0, "boolean": False, "null": None}.get(kind, "mock")


def content_for(response_format: dict | None) -> str:
    spec = (response_format or {}).get("json_schema") or {}
    name = spec.get("name", "")
    if name in _REGISTERED:
        return json.dumps(_REGISTERED[name])
    return json.dumps(instance(spec["schema"])) if "schema" in spec else "{}"


def complete(messages: list[dict], response_format: dict | None, **_: Any):
    """llm.complete's stand-in."""
    from shared.llm.llm import Completion

    wait = LLM_S * random.uniform(0.8, 1.2)
    time.sleep(wait)
    return Completion(content=content_for(response_format), finish_reason="stop", model="mock",
                      prompt_tokens=0, completion_tokens=0, reasoning_tokens=0, gateway_cost_usd=0.0,
                      cost_usd=0.0, duration_sec=round(wait, 3), transport_retries=0, cached=False)
