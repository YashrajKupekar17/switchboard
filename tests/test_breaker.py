"""Phase 2c: circuit breaker. Unit tests for the state machine, then end-to-end through the gateway."""
import httpx
import pytest
import respx

from breaker import CLOSED, HALF_OPEN, NORMAL, OPEN, TRIAL, Breakers, CircuitBreaker
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OLLAMA_URL, OPENAI_URL


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return FakeClock()


def tripped(clock):
    b = CircuitBreaker("openai/gpt-4o-mini", clock=clock)
    for _ in range(5):
        b.record_failure(b.allow())
    return b


# --- the state machine ---


def test_trips_after_5_consecutive_failures_not_4(clock):
    b = CircuitBreaker("x", clock=clock)
    for _ in range(4):
        b.record_failure(b.allow())
    assert b.state == CLOSED
    b.record_failure(b.allow())
    assert b.state == OPEN


def test_a_success_resets_the_count(clock):
    b = CircuitBreaker("x", clock=clock)
    for _ in range(4):
        b.record_failure(b.allow())
    b.record_success(b.allow())
    for _ in range(4):
        b.record_failure(b.allow())
    assert b.state == CLOSED  # never 5 in a row


def test_open_skips_until_cooldown_then_allows_exactly_one_trial(clock):
    b = tripped(clock)
    clock.now += 29.9
    assert b.allow() is None
    clock.now += 0.1
    assert b.allow() == TRIAL
    assert b.state == HALF_OPEN
    assert b.allow() is None  # trial in flight: everyone else still falls back


def test_trial_success_closes(clock):
    b = tripped(clock)
    clock.now += 30
    b.record_success(b.allow())
    assert b.state == CLOSED
    assert b.allow() == NORMAL


def test_trial_failure_reopens_for_a_fresh_cooldown(clock):
    b = tripped(clock)
    clock.now += 30
    b.record_failure(b.allow())
    assert b.state == OPEN
    clock.now += 29
    assert b.allow() is None


def test_abandoned_trial_frees_the_slot_so_the_breaker_cannot_get_stuck(clock):
    b = tripped(clock)
    clock.now += 30
    permit = b.allow()
    b.release(permit)  # e.g. the client disconnected mid-trial
    assert b.allow() == TRIAL


def test_late_results_from_before_the_trip_are_ignored(clock):
    b = CircuitBreaker("x", clock=clock)
    early = b.allow()  # a slow request starts while closed...
    for _ in range(5):
        b.record_failure(b.allow())  # ...meanwhile the breaker trips
    b.record_success(early)  # ...then the slow one finishes
    assert b.state == OPEN  # only a trial can close it


# --- through the gateway ---


@pytest.fixture
def gw(client, clock):
    client.app.state.breakers = Breakers(clock=clock)
    return client


def fail_openai_5_times(gw):
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    openai = respx.post(OPENAI_URL).mock(return_value=httpx.Response(503))
    for _ in range(5):
        gw.post("/v1/chat/completions", json=CHAT_BODY)
    return openai


@respx.mock
def test_open_breaker_skips_the_provider_instantly(gw):
    openai = fail_openai_5_times(gw)

    resp = gw.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "ollama"
    assert resp.headers["x-gateway-attempts"] == "openai:circuit_open"
    assert openai.call_count == 5  # the 6th request never touched openai


@respx.mock
def test_breaker_is_per_model_so_a_healthy_model_keeps_working(gw):
    openai = fail_openai_5_times(gw)  # trips openai/gpt-4o-mini only
    openai.mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))  # openai answers again from now on

    other_model = gw.post("/v1/chat/completions", json={**CHAT_BODY, "model": "gpt-4o"})
    tripped_model = gw.post("/v1/chat/completions", json=CHAT_BODY)

    assert other_model.headers["x-gateway-provider"] == "openai"  # its own breaker never tripped
    assert tripped_model.headers["x-gateway-attempts"] == "openai:circuit_open"  # still in cooldown


@respx.mock
def test_client_errors_never_trip_the_breaker(gw):
    openai = respx.post(OPENAI_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    for _ in range(10):
        gw.post("/v1/chat/completions", json=CHAT_BODY)

    assert openai.call_count == 10  # one buggy team can't cut openai off for everyone


@respx.mock
def test_everything_open_fails_fast_with_503_and_retry_after(gw):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(429))
    for _ in range(5):
        gw.post("/v1/chat/completions", json=CHAT_BODY, headers={"x-gateway-fallback": "off"})

    resp = gw.post("/v1/chat/completions", json=CHAT_BODY, headers={"x-gateway-fallback": "off"})

    assert resp.status_code == 503
    assert resp.headers["x-gateway-attempts"] == "openai:circuit_open"
    assert resp.headers["retry-after"] == "30"


@respx.mock
def test_recovers_after_cooldown_via_a_trial_request(gw, clock):
    openai = fail_openai_5_times(gw)
    openai.mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))  # openai comes back
    clock.now += 30

    trial = gw.post("/v1/chat/completions", json=CHAT_BODY)
    after = gw.post("/v1/chat/completions", json=CHAT_BODY)

    assert trial.headers["x-gateway-provider"] == "openai"
    assert after.headers["x-gateway-provider"] == "openai"
