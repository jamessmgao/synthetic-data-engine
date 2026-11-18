"""
Streaming chat-completions client for the authoring model.

A whole task comes back in one response, which can take many minutes, so the
call streams (no single long-held HTTP read) and retries only on transport
errors. Whether the content is any good is decided by the caller.

Every finished call is written to ``<llm_dir>/<request_sha256>.json`` (request,
response, usage) before it is returned. The hash covers the job's cache key
and the full request, so a job that is retried after a crash reuses the
response instead of paying for it again; only a call that was still in flight
when the process died is repeated.
"""

import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import openai
from openai import OpenAI

from shared.contracts import config

PROGRESS_EVERY_SEC = 30

_TRANSPORT_ERRORS = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.RateLimitError,
    openai.InternalServerError,
)


def _retryable(e: openai.APIError) -> bool:
    """Transport errors, plus errors raised mid-stream by the upstream provider
    (e.g. "Upstream error from Alibaba: Model output became abnormal"). Those
    carry no HTTP status; a 4xx such as a bad request or bad key is final."""
    return isinstance(e, _TRANSPORT_ERRORS) or not isinstance(e, openai.APIStatusError)


@dataclass
class Completion:
    content: str
    finish_reason: str | None
    model: str
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    gateway_cost_usd: float | None
    cost_usd: float
    duration_sec: float
    transport_retries: int
    request_sha256: str = ""
    cached: bool = False  # True when reused from the cache: no new spend

    def to_dict(self) -> dict:
        return asdict(self)

    def usage(self) -> dict:
        """The accounting fields, without the content."""
        d = asdict(self)
        d.pop("content")
        return d


def request_sha256(cache_key: str, request: dict) -> str:
    blob = json.dumps({"cache_key": cache_key, "request": request}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def _write_record(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _client() -> OpenAI:
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY is not set (see .env)")
    return OpenAI(
        base_url=config.OPENROUTER_API_BASE,
        api_key=config.OPENROUTER_API_KEY,
        timeout=config.GENERATOR_TIMEOUT_SEC,
        max_retries=0,  # retried below, so every attempt is visible in the record
    )


def cost_usd(prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * config.GENERATOR_PRICE_IN_PER_M
            + completion_tokens * config.GENERATOR_PRICE_OUT_PER_M) / 1e6


def complete(messages: list[dict], response_format: dict, *, llm_dir: str | Path | None = None,
             cache_key: str = "") -> Completion:
    """One completion from config.MODEL, served from the cache when possible.

    ``cache_key`` should identify the logical call (e.g. the job id plus the
    round), so two different jobs with identical prompts still get separate
    responses, while a retry of the same job reuses its own.
    """
    # The token cap is a client limit, not part of the logical request: raising
    # it must not invalidate finished calls (a truncated one is re-asked anyway).
    request = {"model": config.MODEL, "messages": messages, "response_format": response_format}
    sha = request_sha256(cache_key, request)
    # Calls cached before 2026-11-17 15:30 hashed the cap in (then 65536).
    legacy_sha = request_sha256(cache_key, {**request, "max_tokens": 65536})
    request["max_tokens"] = config.GENERATOR_MAX_TOKENS
    path = Path(llm_dir) / f"{sha}.json" if llm_dir else None
    for candidate in ([path, path.with_name(f"{legacy_sha}.json")] if path else []):
        if candidate.exists():
            record = json.loads(candidate.read_text())
            return Completion(**{**record["completion"], "cached": True})

    completion = _stream(messages, response_format)
    completion.request_sha256 = sha
    if path:
        _write_record(path, {"cache_key": cache_key, "request": request, "completion": completion.to_dict()})
    return completion


def _stream(messages: list[dict], response_format: dict) -> Completion:
    """One streamed completion, retried on transport errors."""
    client = _client()
    retries = 0
    while True:
        start = time.monotonic()
        try:
            stream = client.chat.completions.create(
                model=config.MODEL,
                messages=messages,
                response_format=response_format,
                max_tokens=config.GENERATOR_MAX_TOKENS,
                stream=True,
                stream_options={"include_usage": True},
            )
            parts: list[str] = []
            finish_reason = None
            usage = None
            model = config.MODEL
            reasoning_chars = 0
            next_report = start + PROGRESS_EVERY_SEC
            for chunk in stream:
                model = chunk.model or model
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    parts.append(delta.content or "")
                    reasoning_chars += len((delta.model_extra or {}).get("reasoning") or "")
                    finish_reason = chunk.choices[0].finish_reason or finish_reason
                if chunk.usage:
                    usage = chunk.usage
                if time.monotonic() >= next_report:
                    elapsed = int(time.monotonic() - start)
                    print(f"  [{elapsed}s] receiving: {reasoning_chars} reasoning chars, "
                          f"{sum(map(len, parts))} output chars", file=sys.stderr, flush=True)
                    next_report += PROGRESS_EVERY_SEC
            break
        except openai.APIError as e:
            if not _retryable(e):
                raise
            retries += 1
            if retries > config.GENERATOR_MAX_TRANSPORT_RETRIES:
                raise
            print(f"  retrying after {type(e).__name__}: {str(e)[:160]}", file=sys.stderr, flush=True)
            time.sleep(min(60, 5 * 2 ** retries))

    prompt_tokens = usage.prompt_tokens if usage else 0
    completion_tokens = usage.completion_tokens if usage else 0
    details = getattr(usage, "completion_tokens_details", None)
    extra = (usage.model_extra or {}) if usage else {}
    return Completion(
        content="".join(parts),
        finish_reason=finish_reason,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        reasoning_tokens=(getattr(details, "reasoning_tokens", 0) or 0) if details else 0,
        gateway_cost_usd=extra.get("cost"),
        cost_usd=cost_usd(prompt_tokens, completion_tokens),
        duration_sec=round(time.monotonic() - start, 1),
        transport_retries=retries,
    )
