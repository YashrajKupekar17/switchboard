"""Load shedding (DECISIONS.md Q5): the CoDel-style decision, and the middleware that acts on it."""
import time

import httpx
import respx

import main
from shedding import WINDOW, Overload
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OPENAI_URL


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def overload(target=0.05):
    clock = Clock()
    return Overload(target, clock=clock), clock


def test_fast_requests_never_shed():
    o, clock = overload()
    for _ in range(5):
        o.record(0.002)
        clock.now += WINDOW
    assert not o.shedding()


def test_a_window_where_even_the_fastest_request_waited_sheds_the_next_window():
    o, clock = overload()
    o.record(0.2)
    o.record(0.08)  # the fastest still waited longer than the 50 ms target: a standing queue
    clock.now += WINDOW
    assert o.shedding()


def test_a_burst_is_not_overload():
    o, clock = overload()
    o.record(0.3)  # one request waited behind a burst...
    o.record(0.002)  # ...but another got straight through
    clock.now += WINDOW
    assert not o.shedding()


def test_shedding_ends_once_nothing_is_waiting():
    o, clock = overload()
    o.record(0.2)
    clock.now += WINDOW
    assert o.shedding()
    clock.now += WINDOW  # shedding everything means no new samples: an empty window
    assert not o.shedding()


def test_target_zero_never_sheds():
    o, clock = overload(target=0)
    o.record(5.0)
    clock.now += WINDOW
    assert not o.shedding()


def force_overloaded(monkeypatch):
    # The middleware holds main.OVERLOAD itself, so change that object rather than replace it.
    monkeypatch.setattr(main.OVERLOAD, "target", 0.05)
    monkeypatch.setattr(main.OVERLOAD, "overloaded", True)
    monkeypatch.setattr(main.OVERLOAD, "window_ends", float("inf"))  # stay in this window for the test


@respx.mock
def test_overloaded_copy_refuses_chat_at_once_with_503_and_retry_after(client, monkeypatch):
    force_overloaded(monkeypatch)
    provider = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    assert resp.json()["error"]["type"] == "gateway_overloaded"
    assert provider.call_count == 0
    assert client.get("/healthz").status_code == 200  # only chat is shed: health checks must keep answering


@respx.mock
def test_provider_time_is_not_the_gateways_time(client, monkeypatch):
    samples = []
    monkeypatch.setattr(main.OVERLOAD, "record", samples.append)

    def slow_provider(request):
        time.sleep(0.2)
        return httpx.Response(200, json=FAKE_COMPLETION)

    respx.post(OPENAI_URL).mock(side_effect=slow_provider)

    started = time.perf_counter()
    assert client.post("/v1/chat/completions", json=CHAT_BODY).status_code == 200

    assert time.perf_counter() - started >= 0.2
    assert len(samples) == 1 and samples[0] < 0.1  # a slow provider must never look like an overloaded gateway
