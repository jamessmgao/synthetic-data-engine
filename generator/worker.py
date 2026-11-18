"""
The `generate` job of PRD §3: authoring prompt or previous bundle in, new bundle out.

The Job Service creates the job and decides what happens next; this module
only does the work and reports. It touches no database:

    run_generate_job(job_id, job_input) -> {
        "outcome": "OK" | "FAILED",
        "result": {"name", "bundle_sha256", "bundle_path", "draft_path",
                   "llm_calls": [{phase, round, request_sha256, cached, prompt_tokens,
                                  completion_tokens, cost_usd, duration_sec,
                                  finish_reason, accepted, errors}]},
        "cost_usd": float,   # new spend only; cached calls cost nothing again
        "error": str | None,
    }

job_input, by origin:

    {"origin": "initial", "spec": {...}}          or {"origin": "initial", "prompt": "..."}
    {"origin": "repair", "bundle_sha256": "...", "feedback": "<gate evidence>"}
    {"origin": "revision", "bundle_sha256": "...", "operator": "<PRD §6.6 name>",
     "result": "5 of 5 runs passed.", "evidence": "<one line per run>",
     "direction": "harder" | "easier" | "repair"   (optional; inferred from the operator),
     "analysis": {...}   (optional; the Trace Parser's review of the passing runs:
                          why_easy, simple_strategy, cues, fix_applied, suggestions)}

Idempotent by construction: model calls are cached under work/llm/ by
(job_id, round, request), and bundles are content-addressed under
work/bundles/<sha256>/, so re-running a job after a crash reuses everything
that finished and produces the same bundle.
"""

import ast
import hashlib
import json
import os
import uuid
from pathlib import Path

from pydantic import ValidationError

from shared.contracts import config
from shared.llm import llm
from shared.taskformat.materialize import write_bundle
from generator.prompting import (OPERATORS, REPAIR_REQUEST, continuation, render_initial_prompt, render_revision_request,
                       revision_direction)
from shared.taskformat.schema import BASE_IMAGES, TaskDraft

RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {"name": "task_draft", "strict": True, "schema": TaskDraft.model_json_schema()},
}

FIX_REQUEST = """Your task draft was rejected by the pipeline's checks:

{errors}

Return the complete corrected task as one JSON object matching the schema. Fix
every listed problem and keep everything else that was correct."""

TRUNCATED_REQUEST = """Your previous response was cut off at the output limit
before the JSON was complete. Return the complete task again as one JSON object,
more compactly: keep every required artifact, but trim boilerplate, comments
and any file that is not needed for the task to work."""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _system_prompt() -> str:
    images = ", ".join(f"`{image}`" for image in sorted(BASE_IMAGES))
    return (Path(config.PROMPTS_DIR) / "system.md").read_text().replace("{base_images}", images)


def format_validation_errors(e: ValidationError) -> str:
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "draft"
        for part in err["msg"].removeprefix("Value error, ").split("; "):
            lines.append(f"- {loc}: {part}")
    return "\n".join(lines)


def _repair_guard(previous: TaskDraft, new: TaskDraft) -> str | None:
    """A repair may fix tests but never drop tests or constraints (PRD §6.1)."""

    def test_names(d: TaskDraft) -> set[str]:
        return {n.name for n in ast.walk(ast.parse(d.test_outputs))
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test")}

    problems = []
    dropped_tests = sorted(test_names(previous) - test_names(new))
    if dropped_tests:
        problems.append(f"- the repair removed test functions {dropped_tests}; restore them (fix them instead)")
    dropped = sorted({c.id for c in previous.spec.correctness_constraints}
                     - {c.id for c in new.spec.correctness_constraints})
    if dropped:
        problems.append(f"- the repair removed correctness constraints {dropped}; restore them")
    return "\n".join(problems) or None


def load_draft_record(sha: str, work_dir: Path) -> dict:
    return json.loads((work_dir / "drafts" / f"{sha}.json").read_text())


def _save_draft_record(sha: str, work_dir: Path, record: dict) -> Path:
    path = work_dir / "drafts" / f"{sha}.json"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
        os.replace(tmp, path)
    return path


def _conversation(job_input: dict, work_dir: Path) -> tuple[str, list[dict], TaskDraft | None]:
    """The authoring prompt, the messages to send, and the draft being changed (if any)."""
    origin = job_input["origin"]
    if origin == "initial":
        prompt = job_input.get("prompt") or render_initial_prompt(job_input["spec"])
        return prompt, [{"role": "user", "content": prompt}], None

    parent = load_draft_record(job_input["bundle_sha256"], work_dir)
    previous = TaskDraft.model_validate(parent["draft"])
    if origin == "repair":
        request = REPAIR_REQUEST.format(feedback=job_input["feedback"])
    elif origin == "revision":
        if job_input.get("operator") and job_input["operator"] not in OPERATORS:
            raise ValueError(f"unknown operator {job_input['operator']!r}")
        request = render_revision_request(job_input)
    else:
        raise ValueError(f"unknown origin {origin!r}")
    return parent["prompt"], continuation(parent["prompt"], previous, request), previous


def run_generate_job(job_id: str, job_input: dict, work_dir: str | Path | None = None) -> dict:
    work_dir = Path(work_dir or config.WORK_DIR)
    llm_dir = work_dir / "llm"
    origin = job_input["origin"]
    calls: list[dict] = []

    def report(outcome: str, error: str | None = None, **result) -> dict:
        return {"outcome": outcome, "error": error, "result": {**result, "llm_calls": calls},
                "cost_usd": round(sum(c["cost_usd"] for c in calls if not c["cached"]), 6)}

    try:
        prompt, messages, previous = _conversation(job_input, work_dir)
    except (KeyError, FileNotFoundError, ValueError) as e:
        return report("FAILED", f"bad job input: {type(e).__name__}: {e}")
    system = _system_prompt()
    messages = [{"role": "system", "content": system}, *messages]

    draft: TaskDraft | None = None
    for rnd in range(1, config.GENERATOR_MAX_ATTEMPTS + 1):
        try:
            completion = llm.complete(messages, RESPONSE_FORMAT, llm_dir=llm_dir, cache_key=f"{job_id}/round{rnd}")
        except Exception as e:  # transport retries exhausted, auth failure, ...
            return report("FAILED", f"model call failed: {type(e).__name__}: {e}")

        errors = None
        if completion.finish_reason == "length":
            errors = "truncated: response hit the output token limit"
            messages.append({"role": "user", "content": TRUNCATED_REQUEST})
        else:
            try:
                candidate = TaskDraft.model_validate_json(completion.content)
                errors = _repair_guard(previous, candidate) if origin == "repair" else None
                if errors is None:
                    draft = candidate
            except ValidationError as e:
                errors = format_validation_errors(e)
            if errors:
                messages.append({"role": "assistant", "content": completion.content})
                messages.append({"role": "user", "content": FIX_REQUEST.format(errors=errors)})

        calls.append({"phase": origin, "round": rnd, **completion.usage(), "accepted": errors is None,
                      "errors": errors})
        if draft is not None:
            break

    if draft is None:
        return report("FAILED", f"no acceptable draft after {len(calls)} rounds: {calls[-1]['errors']}")

    meta = {"model": config.MODEL, "origin": origin, "prompt_sha256": _sha256(prompt),
            "system_prompt_sha256": _sha256(system)}
    if origin != "initial":
        meta["parent_bundle_sha256"] = job_input["bundle_sha256"]
    if origin == "revision":
        if job_input.get("operator"):
            meta["operator"] = job_input["operator"]
        meta["direction"] = revision_direction(job_input)
    sha, bundle_path = write_bundle(draft, work_dir / "bundles", meta)
    draft_path = _save_draft_record(sha, work_dir, {"draft": draft.model_dump(), "prompt": prompt,
                                                    "generation_meta": meta, "job_id": job_id})
    return report("OK", name=draft.name, bundle_sha256=sha, bundle_path=str(bundle_path),
                  draft_path=str(draft_path))
