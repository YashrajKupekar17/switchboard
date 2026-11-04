"""Phase 2d: Prometheus metrics. Metrics are global and add up across tests, so every test checks the change."""
import httpx
import respx
from prometheus_client import REGISTRY

from breaker import Breakers
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, FAKE_SSE, OLLAMA_URL, OPENAI_URL, client  # noqa: F401


def value(name, **labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Delta:
    """Reads a metric before and after a block of code."""

    def __init__(self, name, **labels):
        self.name, self.labels = name, labels
        self.before = value(name, **labels)

    @property
    def change(self):
        return value(self.name, **self.labels) - self.before


@respx.mock
def test_metrics_page_is_prometheus_text(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    client.post("/v1/chat/completions", json=CHAT_BODY)

    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert 'gateway_requests_total{model="gpt-4o-mini",status="200"}' in resp.text


@respx.mock
def test_success_counts_request_attempt_tokens_and_latency(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    requests = Delta("gateway_requests_total", model="gpt-4o-mini", status="200")
    attempts = Delta("gateway_upstream_attempts_total", deployment="openai/gpt-4o-mini", outcome="ok")
    prompt = Delta("gateway_tokens_total", deployment="openai/gpt-4o-mini", kind="prompt")
    timed = Delta("gateway_request_duration_seconds_count", model="gpt-4o-mini")

    client.post("/v1/chat/completions", json=CHAT_BODY)

    assert requests.change == 1
    assert attempts.change == 1
    assert prompt.change == 5
    assert timed.change == 1
    assert value("gateway_requests_in_flight") == 0  # back to zero once the request is done


def test_unknown_model_names_share_one_label(client):
    unknown = Delta("gateway_requests_total", model="unknown", status="404")

    client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "made-up-1"})
    client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "made-up-2"})

    assert unknown.change == 2
    assert "made-up" not in client.get("/metrics").text  # a client can't create new time series


def test_invalid_request_is_still_counted(client):
    invalid = Delta("gateway_requests_total", model="unknown", status="422")

    client.post("/v1/chat/completions", json={"messages": []})  # never reaches the endpoint

    assert invalid.change == 1


@respx.mock
def test_fallback_counts_the_failure_and_the_fallback(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(503))
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    failed = Delta("gateway_upstream_attempts_total", deployment="openai/gpt-4o-mini", outcome="server")
    fallbacks = Delta("gateway_fallbacks_total", model="gpt-4o-mini")
    ok = Delta("gateway_requests_total", model="gpt-4o-mini", status="200")

    client.post("/v1/chat/completions", json=CHAT_BODY)

    assert failed.change == 1
    assert fallbacks.change == 1
    assert ok.change == 1  # the client saw success: only the gateway metrics show the degradation


@respx.mock
def test_stream_records_ttft_tokens_and_full_duration(client):
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, content=FAKE_SSE, headers={"content-type": "text/event-stream"})
    )
    ttft = Delta("gateway_ttft_seconds_count", deployment="openai/gpt-4o-mini")
    tpot = Delta("gateway_tpot_seconds_count", deployment="openai/gpt-4o-mini")
    completion = Delta("gateway_tokens_total", deployment="openai/gpt-4o-mini", kind="completion")
    timed = Delta("gateway_request_duration_seconds_count", model="gpt-4o-mini")

    client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})

    assert ttft.change == 1
    assert tpot.change == 1  # 2 completion tokens: one gap after the first token
    assert completion.change == 2  # from the usage chunk at the end of the stream
    assert timed.change == 1


@respx.mock
def test_breaker_state_is_one_hot(client):
    client.app.state.breakers = Breakers()
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(503))
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    for _ in range(5):
        client.post("/v1/chat/completions", json=CHAT_BODY)

    state = {s: value("gateway_breaker_state", deployment="openai/gpt-4o-mini", state=s) for s in ("closed", "half_open", "open")}

    assert state == {"closed": 0, "half_open": 0, "open": 1}
