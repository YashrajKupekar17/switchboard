"""Prometheus metrics. Decisions: DECISIONS.md section H.

Label rule: only labels with a small, fixed set of values (model, deployment, status...), never prompts or
request ids. Every unique label combination is a separate time series that Prometheus keeps in memory.
  model      = the name the client asked for ("gpt-4o-mini", "fast"); unknown names become "unknown"
  deployment = the provider + model we actually called ("openai/gpt-4o-mini"), same as the breakers
"""
import time

from prometheus_client import Counter, Gauge, Histogram

# LLM calls take seconds, not milliseconds: the library's default buckets stop at 10 s.
LATENCY_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 20, 30, 60)
TTFT_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10)
TPOT_BUCKETS = (0.005, 0.01, 0.02, 0.03, 0.05, 0.1, 0.2, 0.5)  # seconds per token: ~20-100 tokens/s is normal

# --- client-facing (RED): what the teams calling us experience ---
REQUESTS = Counter("gateway_requests_total", "Chat requests answered by the gateway", ["model", "status"])
DURATION = Histogram(
    "gateway_request_duration_seconds", "Full request time, including the whole stream", ["model"],
    buckets=LATENCY_BUCKETS,
)
IN_FLIGHT = Gauge("gateway_requests_in_flight", "Chat requests being handled right now")

# --- gateway-specific: problems the client-facing numbers hide ---
ATTEMPTS = Counter(
    "gateway_upstream_attempts_total", "Calls to providers, by result (ok, slow, or the failure kind)",
    ["deployment", "outcome"],
)
FALLBACKS = Counter("gateway_fallbacks_total", "Requests answered by a target other than the first", ["model"])
BREAKER_STATE = Gauge("gateway_breaker_state", "1 for the breaker's current state, 0 for the others", ["deployment", "state"])
TTFT = Histogram("gateway_ttft_seconds", "Time to first token for streams", ["deployment"], buckets=TTFT_BUCKETS)
# TTFT and TPOT don't depend on how long the answer is, so they show a slow provider; total latency can't.
TPOT = Histogram(
    "gateway_tpot_seconds", "Time per output token after the first, for streams", ["deployment"],
    buckets=TPOT_BUCKETS,
)

# --- rate limits (DECISIONS.md K) ---
RATE_LIMITED = Counter("gateway_rate_limited_total", "Requests refused with 429, by team and limit", ["team", "limit"])
RATE_LIMIT_DEGRADED = Gauge("gateway_rate_limit_degraded", "1 while Redis is unreachable and limits are per copy")

# --- budgets (DECISIONS.md L) ---
BUDGET_SPENT = Gauge("gateway_budget_spent_usd", "Spend this month, as of the team's last request", ["team"])
BUDGET_USED = Gauge("gateway_budget_used_ratio", "Spend this month / soft budget (1.0 = at the soft budget)", ["team"])
LEDGER_BUFFERED = Gauge("gateway_ledger_buffered_rows", "Usage rows waiting to be written to Postgres")
LEDGER_DROPPED = Counter("gateway_ledger_dropped_rows_total", "Usage rows lost because the buffer was full")

# --- response cache (DECISIONS.md N) ---
CACHE_LOOKUPS = Counter("gateway_cache_lookups_total", "Cache lookups for requests with caching on", ["model", "result"])
CACHE_STORED = Counter("gateway_cache_stored_total", "Answers written to the cache", ["model"])
CACHE_SAVED = Counter("gateway_cache_saved_usd_total", "What cache hits would have cost at list price", ["team"])

# --- semantic cache (DECISIONS.md O) ---
SEMANTIC_LOOKUPS = Counter(
    "gateway_semantic_lookups_total", "Semantic cache lookups: hit, rejected (the judge said no), miss, shadow_hit, skipped (a call failed)",
    ["model", "result"],
)
# Where a team's best matches sit relative to its threshold: most traffic just under it means it's worth measuring again.
SEMANTIC_SIMILARITY = Histogram(
    "gateway_semantic_similarity", "Similarity of the closest stored question, per lookup", ["team"],
    buckets=(0.5, 0.7, 0.8, 0.85, 0.9, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99, 1.0),
)
# embed: what every lookup pays, hit or miss. judge: what every candidate over the threshold pays.
SEMANTIC_STEP_SECONDS = Histogram(
    "gateway_semantic_step_seconds", "Time of each OpenAI call the semantic cache makes", ["step"],
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1, 1.5, 2),
)

# --- routing (DECISIONS.md P) ---
# reason is one of a fixed set (default, tools, turns, long_prompt), so it's safe as a label. team too: there are
# few, and a rule that suits one team's traffic can waste money on another's (DECISIONS.md P4).
ROUTE_DECISIONS = Counter(
    "gateway_route_decisions_total", "Which model a route picked, and which rule decided",
    ["team", "route", "model", "reason"],
)

# Shadow mode: saved, failed (the other model's call failed or was cut off), skipped (its breaker was open).
ROUTE_SHADOW = Counter("gateway_route_shadow_total", "Shadow answers from a route's other model", ["team", "result"])
ROUTE_SHADOW_USD = Counter("gateway_route_shadow_usd_total", "What shadow answers cost the platform", ["team"])

# --- capacity (DECISIONS.md Q) ---
# The gateway's own time per request: from our middleware until we call the provider or serve a cache hit. Its
# overhead, and the load shedding signal (Q5): a queue anywhere inside this copy shows up here.
GATEWAY_SECONDS = Histogram(
    "gateway_own_seconds", "Time a request spends in the gateway before the provider is called (or a hit is served)",
    buckets=(0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
SHED = Counter("gateway_shed_total", "Chat requests refused with 503 because this copy was overloaded")

# --- cost / usage ---
TOKENS = Counter("gateway_tokens_total", "Tokens used, from the provider's usage report", ["deployment", "kind"])

CHAT_PATH = "/v1/chat/completions"


def record_usage(deployment: str, usage) -> None:
    if usage:
        TOKENS.labels(deployment, "prompt").inc(usage.get("prompt_tokens", 0))
        TOKENS.labels(deployment, "completion").inc(usage.get("completion_tokens", 0))


class RequestMetrics:
    """ASGI middleware for the RED metrics.

    It wraps the whole response, so for a stream it only finishes after the last chunk is sent,
    which a normal FastAPI dependency or decorator can't see. The endpoint tells it the model label
    through request.state (stored in scope["state"]).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] != CHAT_PATH:
            return await self.app(scope, receive, send)

        status = 500  # if the app crashes before sending anything, the client sees a 500
        started = time.perf_counter()

        async def send_and_watch(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        IN_FLIGHT.inc()
        try:
            await self.app(scope, receive, send_and_watch)
        finally:
            IN_FLIGHT.dec()
            model = scope.get("state", {}).get("metrics_model", "unknown")  # e.g. a 422 never reaches the endpoint
            REQUESTS.labels(model, str(status)).inc()
            DURATION.labels(model).observe(time.perf_counter() - started)
