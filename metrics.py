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
    "gateway_upstream_attempts_total", "Calls to providers, by result (ok, or the failure kind)",
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
