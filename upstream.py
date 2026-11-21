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
from typing import Callable, Optional, Union

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

import anthropic_adapter
from anthropic_adapter import StreamError, Unsupported
from cache import assemble
from config import Target
from metrics import ATTEMPTS, TPOT, TTFT, record_usage
from transport import ProviderFull

log = logging.getLogger("gateway")

# pool = how long to wait for a free connection. Waiting means this copy is full (DECISIONS.md Q2), and the
# request would only get slower, so give up after 1 s and let the client retry, likely on another copy.
POOL_WAIT = 1.0
NON_STREAM_TIMEOUT = httpx.Timeout(connect=3.0, read=60.0, write=10.0, pool=POOL_WAIT)
STREAM_TIMEOUT = httpx.Timeout(connect=3.0, read=30.0, write=10.0, pool=POOL_WAIT)  # read = max gap between chunks
FIRST_CHUNK_TIMEOUT = 10.0  # seconds to wait for a stream's first chunk; until then we can still fall back

# Failure kinds that are safe to retry: the provider either never got the request or failed fast.
RETRYABLE = {"server", "connect"}
# Failure kinds that never reached the provider, or are our own limit: they say nothing about its health, so they
# never count against its breaker.
NOT_ITS_FAULT = {"unsupported", "overloaded", "provider_full"}

# How long a provider made us wait, for the slow-call check (DECISIONS.md R3). For a stream, until its first
# chunk. A non-streamed answer arrives all at once, so we take the whole time and allow this much per output token
# for writing it: 20 tokens/s, slower than any model we route to. What's left is time spent waiting to start.
WRITING_S_PER_TOKEN = 0.05

# What the client gets when every target failed, keyed by the last failure's kind.
FINAL_STATUS = {
    "auth": 502, "config": 502, "rate_limit": 503, "server": 502, "connect": 502, "network": 502, "timeout": 504,
    "circuit_open": 503,  # we skipped it ourselves; Retry-After says when the next trial is
    "unsupported": 400,  # no target could handle the request's features (e.g. tools on an Anthropic-only model)
    "overloaded": 503,  # no free connection in this copy: our limit, not the provider's
    "provider_full": 503,  # this provider has its share of our connections (R2), and no other target could take it
}


@dataclass
class Failure:
    kind: str  # "auth" | "config" | "rate_limit" | "server" | "connect" | "network" | "timeout" | "circuit_open"
    #            "unsupported" | "overloaded" | "provider_full"
    detail: str
    retry_after: Optional[str] = None


def classify_status(status: int) -> Optional[str]:
    """Failure kind for a provider status code, or None if the response should go to the client as-is."""
    if status in (401, 403):
        return "auth"  # our provider key is broken: the platform's fault, not the client's
    if status == 404:
        return "config"  # the provider doesn't know the model ID in our config.yaml: our fault, not the client's
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    return None  # 2xx, or a request error the client must fix


def classify_exception(exc: Exception) -> Failure:
    if isinstance(exc, ProviderFull):
        # Our cap on this one provider (R2). Another provider may have room, so the caller falls back.
        return Failure("provider_full", repr(exc), retry_after="1")
    if isinstance(exc, httpx.PoolTimeout):
        # Every connection in our pool was busy. The provider never saw the request and may be perfectly fine:
        # blaming it would open its breaker and turn our overload into an outage (DECISIONS.md Q2).
        return Failure("overloaded", repr(exc), retry_after="1")
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return Failure("connect", repr(exc))  # request never reached the provider
    if isinstance(exc, httpx.TimeoutException):
        return Failure("timeout", repr(exc))  # provider got it but is too slow; retrying piles on load
    return Failure("network", repr(exc))  # connection broke after sending; unsafe to retry


UsageCallback = Callable[[Optional[dict]], None]
CompleteCallback = Optional[Callable[[dict], None]]  # gets the whole answer, OpenAI format, if it finished cleanly
WaitedCallback = Callable[[float], None]  # gets the seconds the provider made us wait (WRITING_S_PER_TOKEN)


def _ignore_usage(usage: Optional[dict]) -> None:
    pass


def _ignore_waited(seconds: float) -> None:
    pass


def waited(seconds: float, usage: Optional[dict]) -> float:
    """For a non-streamed answer: its time, less what writing its tokens is allowed to take."""
    return seconds - ((usage or {}).get("completion_tokens") or 0) * WRITING_S_PER_TOKEN


async def call_target(
    http: httpx.AsyncClient, target: Target, payload: dict, stream: bool, on_usage: UsageCallback = _ignore_usage,
    on_complete: CompleteCallback = None, on_waited: WaitedCallback = _ignore_waited, forward_usage: bool = True,
) -> Union[Response, Failure]:
    """on_usage gets the provider's usage report once the answer is complete (None if it never arrived).
    on_complete gets the whole answer, only if it finished cleanly; for a stream that means [DONE] arrived.
    on_waited gets how long the provider made us wait, before this returns, for answers that arrived.
    forward_usage=False keeps a stream's usage-only chunk from the client (it's still read, for billing)."""
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
        return await _call_stream(http, target, payload, headers, on_usage, on_complete, on_waited, forward_usage)

    started = time.perf_counter()
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
    on_waited(waited(time.perf_counter() - started, body.get("usage") if upstream.is_success else None))
    if upstream.is_success:
        record_usage(target.deployment, body.get("usage"))
        on_usage(body.get("usage"))
        if on_complete:
            on_complete(body)
    return JSONResponse(status_code=upstream.status_code, content=body, headers=headers)


async def _call_stream(
    http: httpx.AsyncClient, target: Target, payload: dict, headers: dict, on_usage: UsageCallback,
    on_complete: CompleteCallback, on_waited: WaitedCallback, forward_usage: bool,
):
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
    on_waited(time.perf_counter() - started)
    lines, buffered = first
    return StreamingResponse(
        relay_stream(_replay(buffered, lines), started, target.deployment, on_usage, on_complete, forward_usage),
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


async def relay_stream(
    lines, started: float, deployment: str, on_usage: UsageCallback = _ignore_usage, on_complete: CompleteCallback = None,
    forward_usage: bool = True,
):
    """Forward SSE events to the client unchanged, while watching them for TTFT and usage. With forward_usage=False,
    the usage-only chunk (empty `choices`) is read but not sent: the client didn't ask for it."""
    first_token_at = None
    usage = None
    chunks = [] if on_complete else None  # kept only when someone wants the whole answer (the cache)
    finished = False
    # An SSE event is its lines up to a blank line, and a client can't use it before then. So send each event in
    # one write, not one per line: every write goes through Starlette, uvicorn and the socket, and per-line writes
    # doubled the cost of a stream (DECISIONS.md Q4).
    event = []
    hold_back = False  # the current event is the usage report the client didn't ask for

    try:
        async for line in lines:
            if line == "data: [DONE]":
                finished = True
            elif line.startswith("data: "):
                chunk = json.loads(line[len("data: "):])
                if first_token_at is None and any(c["delta"].get("content") for c in chunk.get("choices", [])):
                    first_token_at = time.perf_counter()
                if chunk.get("usage"):
                    usage = chunk["usage"]
                    hold_back = not forward_usage and not chunk.get("choices")
                if chunks is not None:
                    chunks.append(chunk)
            event.append(line + "\n")
            if line == "":
                if not hold_back:
                    yield "".join(event)
                event, hold_back = [], False
        if event and not hold_back:  # the provider ended without a final blank line
            yield "".join(event)
    except (httpx.HTTPError, StreamError) as exc:
        # Lines of an unfinished event are dropped: sent before the error event, they'd merge into it (an SSE
        # event's data lines are joined) and break it.
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
        on_usage(usage)
        if finished and chunks:  # a stream that broke or was abandoned is never handed on
            on_complete(assemble(chunks))
        log.info(
            "stream done ttft=%s total=%.3fs usage=%s",
            f"{ttft:.3f}s" if ttft is not None else "n/a",
            total,
            usage,
        )
