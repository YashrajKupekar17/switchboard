import asyncio
import datetime
import logging
import math
import os
import random
from contextlib import asynccontextmanager
from typing import Optional

import httpx
import redis.asyncio as redis
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel, ConfigDict

import budgets
from breaker import Breakers
import keys
import pricing
import ratelimit
from config import load_config
from metrics import ATTEMPTS, FALLBACKS, RATE_LIMITED, RequestMetrics
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


async def open_key_store():
    # Required: without a key store nobody can be authenticated, so fail at startup, not on the first request.
    return await keys.connect(os.environ["DATABASE_URL"])


async def open_ledger(key_store):
    # The ledger shares the key store's Postgres pool: same database, no second set of connections.
    return await budgets.open_ledger(key_store.pool)


def open_redis() -> redis.Redis:
    # Connects lazily, so the gateway starts even if Redis is down (rate limits fail open, DECISIONS.md K4).
    # Short timeouts: Redis answers in well under a millisecond, so a slow answer means it's in trouble.
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return redis.from_url(url, socket_timeout=0.1, socket_connect_timeout=0.1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # One shared client for the whole app = one connection pool, reused by every request.
    app.state.http = httpx.AsyncClient()
    app.state.breakers = Breakers()  # in this process's memory, by design (DECISIONS.md G4)
    app.state.key_store = await open_key_store()
    app.state.keys = keys.KeyCache(app.state.key_store)  # in-process cache in front of Postgres (J2)
    shared = open_redis()  # one Redis connection pool for rate limits and budgets
    app.state.limiter = ratelimit.RateLimiter(shared, CONFIG.limits_for, CONFIG.gateway_copies)
    app.state.budgets = budgets.Budgets(shared, await open_ledger(app.state.key_store), CONFIG.budget_for)
    app.state.budgets.start()  # writes buffered usage rows to Postgres every second
    yield
    await app.state.http.aclose()
    await app.state.budgets.stop()  # before the pool closes: writes the last rows
    await app.state.key_store.close()
    await app.state.limiter.close()


_settling = set()  # keeps a reference to each settle task until it finishes, so it isn't garbage collected


app = FastAPI(lifespan=lifespan)
app.add_middleware(RequestMetrics)


class AuthError(Exception):
    def __init__(self, status: int, message: str, code: str):
        self.status, self.message, self.code = status, message, code


@app.exception_handler(AuthError)
async def auth_error(request: Request, exc: AuthError):
    # Same shape and code as OpenAI's own 401, so SDKs raise their usual AuthenticationError.
    return JSONResponse(
        status_code=exc.status,
        content={"error": {"message": exc.message, "type": "invalid_request_error", "code": exc.code}},
    )


async def require_key(request: Request, authorization: Optional[str] = Header(default=None)) -> keys.KeyInfo:
    """First step of every request: which team is calling? (DECISIONS.md J)"""
    scheme, _, key = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not key:
        raise AuthError(401, "missing API key: send 'Authorization: Bearer sbk_...'", "missing_api_key")
    try:
        info = await request.app.state.keys.check(key.strip())
    except keys.AuthUnavailable as exc:
        log.error("key lookup failed, rejecting request: %s", exc)  # fail closed: never let unknown keys in
        raise AuthError(503, "authentication is temporarily unavailable", "auth_unavailable")
    if info is None:
        raise AuthError(401, "invalid or revoked API key", "invalid_api_key")
    request.state.team = info.team
    return info


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/metrics")
def metrics():
    # Current totals as plain text. Prometheus fetches this every 15 s and keeps the history.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/v1/chat/completions")
async def chat_completions(
    request: ChatRequest,
    http_request: Request,
    caller: keys.KeyInfo = Depends(require_key),
    x_gateway_fallback: Optional[str] = Header(default=None),
):
    payload = request.model_dump(exclude_none=True)

    # Rate limit right after auth, before anything that costs money or time (DECISIONS.md K).
    limits = CONFIG.limits_for(caller.team)
    estimate = ratelimit.estimate_tokens(payload["messages"])
    decision = await app.state.limiter.check(caller.team, estimate)
    if not decision.allowed:
        RATE_LIMITED.labels(caller.team, decision.exceeded).inc()
        return _rate_limited(caller.team, limits, decision, estimate)

    # Then the monthly budget (DECISIONS.md L).
    budget = CONFIG.budget_for(caller.team)
    spend = await app.state.budgets.check(caller.team)
    if not spend.allowed:
        RATE_LIMITED.labels(caller.team, "budget").inc()
        return _over_budget(caller.team, budget, spend)

    def settle(target, usage):
        # Called with the provider's real usage once the answer is done (for a stream, after the last chunk).
        task = asyncio.create_task(_settle(caller, request.model, target, estimate, usage))
        _settling.add(task)
        task.add_done_callback(_settling.discard)

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
                result = await call_target(
                    app.state.http, target, payload, request.stream, on_usage=lambda u, t=target: settle(t, u)
                )
            finally:
                if result is None:  # cancelled or crashed before the provider gave a verdict
                    breaker.release(permit)

            if not isinstance(result, Failure):
                breaker.record_success(permit)  # any real answer (even a 400) proves the provider is up
                ATTEMPTS.labels(target.deployment, "ok").inc()
                if i > 0:
                    FALLBACKS.labels(request.model).inc()
                log.info("route team=%s model=%s -> %s/%s", caller.team, request.model, target.provider.name, target.model)
                if failures:
                    result.headers["x-gateway-attempts"] = _attempts(failures)
                result.headers.update(_limit_headers(limits, decision))
                result.headers.update(_budget_headers(budget, spend))
                return result

            ATTEMPTS.labels(target.deployment, result.kind).inc()
            failures.append((target, result))
            if result.kind == "unsupported":
                breaker.release(permit)  # never reached the provider: says nothing about its health
                break

            breaker.record_failure(permit)
            level = logging.ERROR if result.kind in ("auth", "config") else logging.WARNING  # our own setup is broken: alert
            log.log(level, "attempt failed %s/%s kind=%s %s", target.provider.name, target.model, result.kind, result.detail)

            # With another target available, move on immediately. On the last one, retry once if it's safe.
            if is_last and attempt == 1 and result.kind in RETRYABLE:
                await asyncio.sleep(random.uniform(0.1, 0.3))  # jitter: don't retry in lockstep with everyone else
                continue
            break

    return _all_failed(request.model, failures)


async def _settle(caller: keys.KeyInfo, model: str, target, estimate: int, usage: Optional[dict]) -> None:
    await app.state.limiter.settle(caller.team, estimate, usage)
    estimated = not usage
    if estimated:  # stream cut short before the usage report: charge the prompt estimate, and say so
        usage = {"prompt_tokens": estimate, "completion_tokens": 0}
    await app.state.budgets.record(
        team=caller.team, key_id=caller.id, model=model, deployment=target.deployment,
        tokens=pricing.split_usage(usage), cost_micros=pricing.cost_micros(CONFIG.prices[target.deployment], usage),
        estimated=estimated,
    )


def _budget_headers(budget: budgets.Budget, spend: budgets.Spend) -> dict:
    if spend.spent_micros is None:  # Redis down: we don't know, so we don't pretend to
        return {}
    return {
        "x-gateway-budget-spent-usd": pricing.micros_to_usd(spend.spent_micros),
        "x-gateway-budget-soft-usd": pricing.usd(budget.soft_usd)[1:],
        "x-gateway-budget-max-usd": pricing.usd(budget.max_usd)[1:],
    }


def _over_budget(team: str, budget: budgets.Budget, spend: budgets.Spend) -> JSONResponse:
    now = budgets.utcnow()
    next_month = (budgets.month_start(now) + datetime.timedelta(days=32)).replace(day=1)
    headers = _budget_headers(budget, spend)
    headers["Retry-After"] = str(int((next_month - now).total_seconds()))
    return JSONResponse(
        status_code=429,
        # OpenAI uses insufficient_quota for "you're out of credit", which is what this is.
        content={"error": {
            "message": f"team {team!r} has reached its monthly budget of {pricing.usd(budget.max_usd)}; "
                       f"it resets on {next_month:%Y-%m-%d} (UTC)",
            "type": "insufficient_quota",
            "code": "budget_exceeded",
        }},
        headers=headers,
    )


def _limit_headers(limits: ratelimit.Limits, decision: ratelimit.Decision) -> dict:
    # The same headers OpenAI sends, so clients that already read them work unchanged.
    return {
        "x-ratelimit-limit-requests": str(limits.rpm),
        "x-ratelimit-remaining-requests": str(decision.remaining_requests),
        "x-ratelimit-limit-tokens": str(limits.tpm),
        "x-ratelimit-remaining-tokens": str(decision.remaining_tokens),
    }


def _rate_limited(team: str, limits: ratelimit.Limits, decision: ratelimit.Decision, estimate: int) -> JSONResponse:
    if decision.exceeded == "tokens" and estimate > limits.tpm:
        # It would never fit, however long the client waits. OpenAI also answers this with a 429.
        message = f"request too large for team {team!r}: about {estimate} tokens, limit {limits.tpm} per minute"
    else:
        per_minute = limits.rpm if decision.exceeded == "requests" else limits.tpm
        message = (f"rate limit reached for team {team!r} on {decision.exceeded} per minute: "
                   f"limit {per_minute}, try again in {decision.retry_after:.1f}s")
    headers = _limit_headers(limits, decision)
    headers["Retry-After"] = str(max(1, math.ceil(decision.retry_after)))  # the OpenAI SDK waits this long, then retries
    return JSONResponse(
        status_code=429,
        content={"error": {"message": message, "type": decision.exceeded, "code": "rate_limit_exceeded"}},
        headers=headers,
    )


def _attempts(failures: list) -> str:
    return ", ".join(f"{t.provider.name}:{f.kind}" for t, f in failures)


def _all_failed(model: str, failures: list) -> JSONResponse:
    # A real provider failure outranks "unsupported": a 400 would wrongly blame the client.
    last = next((f for _, f in reversed(failures) if f.kind != "unsupported"), failures[-1][1])
    headers = {"x-gateway-attempts": _attempts(failures)}
    if last.kind in ("rate_limit", "circuit_open"):
        headers["Retry-After"] = last.retry_after or "1"
    unsupported = last.kind == "unsupported"
    return JSONResponse(
        status_code=FINAL_STATUS[last.kind],
        content={"error": {
            "message": last.detail if unsupported else f"all providers failed for model {model!r}",
            "type": "invalid_request_error" if unsupported else "upstream_error",
            "attempts": [{"provider": t.provider.name, "kind": f.kind, "detail": f.detail} for t, f in failures],
        }},
        headers=headers,
    )
