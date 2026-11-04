import asyncio
import logging
import os
import random
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict

from breaker import Breakers
from config import load_config
from metrics import ATTEMPTS, FALLBACKS, RequestMetrics
from upstream import FINAL_STATUS, RETRYABLE, Failure, call_target

load_dotenv()  # reads .env into os.environ (real env vars win if both are set)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gateway")

CONFIG = load_config(os.environ.get("GATEWAY_CONFIG", "config.yaml"))


class Message(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str  # "system" | "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    # Keep fields we don't model (top_p, tools, ...) so they still reach the provider.
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[Message]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared client for the whole app = one connection pool, reused by every request.
    app.state.http = httpx.AsyncClient()
    app.state.breakers = Breakers()  # in this process's memory, by design (DECISIONS.md G4)
    yield
    await app.state.http.aclose()


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestMetrics)


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    # Current totals as plain text. Prometheus fetches this every 15 s and keeps the history.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatRequest, http_request: Request, x_gateway_fallback: Optional[str] = Header(default=None)
):
    targets = CONFIG.models.get(request.model)
    # Only configured names become a label: clients can send any string, and each one would be a new time series.
    http_request.state.metrics_model = request.model if targets else "unknown"
    if not targets:
        return JSONResponse(
            status_code=404,
            content={"error": {
                "message": f"model {request.model!r} is not configured on this gateway",
                "type": "invalid_request_error",
                "code": "model_not_found",
            }},
        )
    if x_gateway_fallback == "off":  # opt-out: the client wants exactly the first target or an error
        targets = targets[:1]

    payload = request.model_dump(exclude_none=True)
    if request.stream:
        # Providers only send token usage at the end of a stream if asked to; we need it for cost tracking.
        payload["stream_options"] = {**payload.get("stream_options", {}), "include_usage": True}

    failures = []  # (target, Failure) for every attempt that failed
    for i, target in enumerate(targets):
        is_last = i == len(targets) - 1
        breaker = app.state.breakers.get(target.provider.name, target.model)
        for attempt in (1, 2):
            permit = breaker.allow()
            if permit is None:  # known to be failing: skip it instantly instead of waiting for it to fail again
                ATTEMPTS.labels(target.deployment, "circuit_open").inc()
                failures.append((target, Failure("circuit_open", "skipped: breaker open", str(breaker.seconds_until_trial()))))
                break

            result = None
            try:
                result = await call_target(app.state.http, target, payload, request.stream)
            finally:
                if result is None:  # cancelled or crashed before the provider gave a verdict
                    breaker.release(permit)

            if not isinstance(result, Failure):
                breaker.record_success(permit)  # any real answer (even a 400) proves the provider is up
                ATTEMPTS.labels(target.deployment, "ok").inc()
                if i > 0:
                    FALLBACKS.labels(request.model).inc()
                log.info("route model=%s -> %s/%s", request.model, target.provider.name, target.model)
                if failures:
                    result.headers["x-gateway-attempts"] = _attempts(failures)
                return result

            breaker.record_failure(permit)
            ATTEMPTS.labels(target.deployment, result.kind).inc()
            failures.append((target, result))
            level = logging.ERROR if result.kind == "auth" else logging.WARNING  # auth = our key is broken: alert
            log.log(level, "attempt failed %s/%s kind=%s %s", target.provider.name, target.model, result.kind, result.detail)

            # With another target available, move on immediately. On the last one, retry once if it's safe.
            if is_last and attempt == 1 and result.kind in RETRYABLE:
                await asyncio.sleep(random.uniform(0.1, 0.3))  # jitter: don't retry in lockstep with everyone else
                continue
            break

    return _all_failed(request.model, failures)


def _attempts(failures: list) -> str:
    return ", ".join(f"{t.provider.name}:{f.kind}" for t, f in failures)


def _all_failed(model: str, failures: list) -> JSONResponse:
    last = failures[-1][1]
    headers = {"x-gateway-attempts": _attempts(failures)}
    if last.kind in ("rate_limit", "circuit_open"):
        headers["Retry-After"] = last.retry_after or "1"
    return JSONResponse(
        status_code=FINAL_STATUS[last.kind],
        content={"error": {
            "message": f"all providers failed for model {model!r}",
            "type": "upstream_error",
            "attempts": [{"provider": t.provider.name, "kind": f.kind, "detail": f.detail} for t, f in failures],
        }},
        headers=headers,
    )
