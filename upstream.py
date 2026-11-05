"""Calling one provider target, and deciding what a failure means.

The policy (see DESIGN.md section 4):
  request errors (400...)  -> return to client as-is, never fall back
  401/403, 429, 5xx, connect errors, read timeouts -> provider problem -> try the next target
  only the last target gets one retry, and only when nothing was sent (5xx, connect errors)

Providers with `type: anthropic` are translated to and from OpenAI's format here (anthropic_adapter.py).
"""
import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional, Union

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import anthropic_adapter
from anthropic_adapter import StreamError, Unsupported
from config import Target
from metrics import ATTEMPTS, TPOT, TTFT, record_usage

log = logging.getLogger("gateway")

NON_STREAM_TIMEOUT = httpx.Timeout(connect=3.0, read=60.0, write=10.0, pool=5.0)
STREAM_TIMEOUT = httpx.Timeout(connect=3.0, read=30.0, write=10.0, pool=5.0)  # read = max gap between chunks
FIRST_CHUNK_TIMEOUT = 10.0  # seconds to wait for a stream's first chunk; until then we can still fall back

# Failure kinds that are safe to retry: the provider either never got the request or failed fast.
RETRYABLE = {"server", "connect"}

# What the client gets when every target failed, keyed by the last failure's kind.
FINAL_STATUS = {
    "auth": 502, "rate_limit": 503, "server": 502, "connect": 502, "network": 502, "timeout": 504,
    "circuit_open": 503,  # we skipped it ourselves; Retry-After says when the next trial is
    "unsupported": 400,  # no target could handle the request's features (e.g. tools on an Anthropic-only model)
}


@dataclass
class Failure:
    kind: str  # "auth" | "rate_limit" | "server" | "connect" | "network" | "timeout" | "circuit_open" | "unsupported"
    detail: str
    retry_after: Optional[str] = None


def classify_status(status: int) -> Optional[str]:
    """Failure kind for a provider status code, or None if the response should go to the client as-is."""
    if status in (401, 403):
        return "auth"  # our provider key is broken: the platform's fault, not the client's
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    return None  # 2xx, or a request error the client must fix


def classify_exception(exc: Exception) -> Failure:
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)):
        return Failure("connect", repr(exc))  # request never reached the provider
    if isinstance(exc, httpx.TimeoutException):
        return Failure("timeout", repr(exc))  # provider got it but is too slow; retrying piles on load
    return Failure("network", repr(exc))  # connection broke after sending; unsafe to retry


async def call_target(
    http: httpx.AsyncClient, target: Target, payload: dict, stream: bool
) -> Union[Response, Failure]:
    payload = {**payload, "model": target.model}  # the provider's own name for the model
    headers = {"x-gateway-provider": target.provider.name}
    is_anthropic = target.provider.type == "anthropic"
    if is_anthropic:
        try:
            payload, dropped = anthropic_adapter.to_anthropic(payload)
        except Unsupported as exc:
            return Failure("unsupported", str(exc))  # skip this target; another one may handle it
        if dropped:
            headers["x-gateway-dropped-params"] = ",".join(dropped)  # tell the client, don't hide it
    if stream:
        return await _call_stream(http, target, payload, headers)

    try:
        upstream = await http.post(
            target.provider.chat_url, headers=target.provider.headers(), json=payload, timeout=NON_STREAM_TIMEOUT
        )
    except httpx.HTTPError as exc:
        return classify_exception(exc)

    kind = classify_status(upstream.status_code)
    if kind:
        return Failure(kind, f"HTTP {upstream.status_code}", upstream.headers.get("retry-after"))
    if is_anthropic:
        body = anthropic_adapter.from_anthropic(upstream.json()) if upstream.is_success else (
            anthropic_adapter.error_to_openai(upstream.content))
    else:
        body = upstream.json()
    if upstream.is_success:
        record_usage(target.deployment, body.get("usage"))
    return JSONResponse(status_code=upstream.status_code, content=body, headers=headers)


async def _call_stream(http: httpx.AsyncClient, target: Target, payload: dict, headers: dict):
    started = time.perf_counter()
    try:
        upstream, error_body, first = await asyncio.wait_for(
            _open_stream(http, target, payload), timeout=FIRST_CHUNK_TIMEOUT
        )
    except asyncio.TimeoutError:
        return Failure("timeout", f"no first chunk within {FIRST_CHUNK_TIMEOUT}s")
    except httpx.HTTPError as exc:
        return classify_exception(exc)
    except StopAsyncIteration:
        return Failure("network", "stream closed before first chunk")
    except StreamError as exc:  # an error inside the 200 stream, before we committed: still safe to fall back
        return Failure(exc.kind, str(exc))

    if error_body is not None:
        kind = classify_status(upstream.status_code)
        if kind:
            return Failure(kind, f"HTTP {upstream.status_code}", upstream.headers.get("retry-after"))
        is_anthropic = target.provider.type == "anthropic"
        content = anthropic_adapter.error_to_openai(error_body) if is_anthropic else json.loads(error_body)
        return JSONResponse(status_code=upstream.status_code, content=content, headers=headers)

    # First chunk is in hand: only now do we commit to this provider and send 200 to the client.
    lines, buffered = first
    return StreamingResponse(
        relay_stream(_replay(buffered, lines), started, target.deployment),
        media_type="text/event-stream",
        headers=headers,
        background=BackgroundTask(upstream.aclose),  # release the connection even if the client disconnects
    )


async def _open_stream(http: httpx.AsyncClient, target: Target, payload: dict):
    """Send the request and read ahead until the first data chunk, without sending anything to the client."""
    request = http.build_request(
        "POST", target.provider.chat_url, headers=target.provider.headers(), json=payload, timeout=STREAM_TIMEOUT
    )
    upstream = await http.send(request, stream=True)
    try:
        if upstream.status_code != 200:
            body = await upstream.aread()
            await upstream.aclose()
            return upstream, body, None

        lines = upstream.aiter_lines()
        if target.provider.type == "anthropic":
            lines = anthropic_adapter.translate_stream(lines)  # from here on, every line is OpenAI-format
        buffered = []
        while True:
            line = await lines.__anext__()  # StopAsyncIteration if the stream ends with no data
            buffered.append(line)
            if line.startswith("data: "):
                return upstream, None, (lines, buffered)
    except BaseException:  # includes cancellation by the first-chunk timeout
        await upstream.aclose()
        raise


async def _replay(buffered: list, rest):
    for line in buffered:
        yield line
    async for line in rest:
        yield line


async def relay_stream(lines, started: float, deployment: str):
    """Forward SSE lines to the client unchanged, while watching them for TTFT and usage."""
    first_token_at = None
    usage = None

    try:
        async for line in lines:
            if line.startswith("data: ") and line != "data: [DONE]":
                chunk = json.loads(line[len("data: "):])
                if first_token_at is None and any(c["delta"].get("content") for c in chunk.get("choices", [])):
                    first_token_at = time.perf_counter()
                if chunk.get("usage"):
                    usage = chunk["usage"]
            yield line + "\n"
    except (httpx.HTTPError, StreamError) as exc:
        # Provider died mid-stream. The 200 and some chunks are already sent, so we can't fall back.
        # Send an error event (the OpenAI SDK raises APIError on it) so the client knows the answer is incomplete.
        log.warning("upstream failed mid-stream: %r", exc)
        ATTEMPTS.labels(deployment, "mid_stream").inc()  # visible on the dashboard, though it doesn't trip the breaker
        error = {"error": {"message": "upstream provider failed mid-stream", "type": "upstream_error"}}
        yield f"data: {json.dumps(error)}\n\n"
    finally:
        # Runs even when the client disconnects and the server cancels this generator,
        # so usage is never silently lost. (usage=None means the client left before the provider sent it.)
        total = time.perf_counter() - started
        ttft = (first_token_at - started) if first_token_at else None
        if ttft is not None:
            TTFT.labels(deployment).observe(ttft)
            tokens = (usage or {}).get("completion_tokens", 0)
            if tokens > 1:  # needs the token count, so a client that leaves early gives no TPOT
                TPOT.labels(deployment).observe((total - ttft) / (tokens - 1))
        record_usage(deployment, usage)
        log.info(
            "stream done ttft=%s total=%.3fs usage=%s",
            f"{ttft:.3f}s" if ttft is not None else "n/a",
            total,
            usage,
        )
