import json
import time

import httpx
import pytest
import respx

import budgets
import main
from cache import CacheSettings, assemble, cache_key, replay, settings_from, storable
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OLLAMA_URL, OPENAI_URL

CACHE_ON = {"x-gateway-cache": "on"}


def wait_for(condition, timeout=2.0):
    # Storing and settling happen in background tasks after the response is sent.
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def stored_keys(server_client):
    return server_client.portal.call(main.app.state.cache.redis.keys, "cache:*")


# --- the key ---

def test_key_ignores_field_order():
    a = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    b = {"temperature": 0, "messages": [{"content": "hi", "role": "user"}], "model": "m"}
    assert cache_key("t", a) == cache_key("t", b)


def test_key_ignores_delivery_fields():
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    streamed = {**base, "stream": True, "stream_options": {"include_usage": True}, "user": "u-42"}
    assert cache_key("t", base) == cache_key("t", streamed)


@pytest.mark.parametrize("change", [
    {"temperature": 0.5}, {"max_tokens": 10}, {"model": "other"}, {"tools": [{"type": "function"}]},
    {"messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]},
    {"some_field_openai_adds_next_year": 1},  # unknown fields are in the key: a miss, never a wrong hit
])
def test_anything_that_can_change_the_answer_changes_the_key(change):
    base = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "temperature": 0}
    assert cache_key("t", base) != cache_key("t", {**base, **change})


def test_teams_never_share_keys():
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    assert cache_key("support", body) != cache_key("ads", body)


# --- what gets stored ---

@pytest.mark.parametrize("finish, ok", [("stop", True), ("tool_calls", True), ("length", False),
                                        ("content_filter", False), (None, False)])
def test_only_complete_answers_are_storable(finish, ok):
    assert storable({"choices": [{"finish_reason": finish}]}) is ok


def test_header_overrides_team_default():
    off = CacheSettings(enabled=False, ttl=3600)
    assert settings_from(off, "on", None).enabled
    assert not settings_from(CacheSettings(True, 3600), "off", None).enabled
    assert settings_from(off, None, None) == off
    assert settings_from(off, "on", "60").ttl == 60
    assert settings_from(off, "on", "99999999").ttl == 7 * 86400  # capped
    assert settings_from(off, "on", "soon").ttl == 3600  # ignored if not a number


# --- streams: assemble and replay ---

STREAM_CHUNKS = [
    {"id": "c1", "created": 1, "model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]},
    {"id": "c1", "created": 1, "model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"content": "Hel"}}]},
    {"id": "c1", "created": 1, "model": "gpt-4o-mini", "choices": [{"index": 0, "delta": {"content": "lo"}, "finish_reason": "stop"}]},
    {"id": "c1", "created": 1, "model": "gpt-4o-mini", "choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}},
]


def test_assemble_rebuilds_the_non_stream_answer():
    body = assemble(STREAM_CHUNKS)
    assert body["choices"] == [{"index": 0, "message": {"role": "assistant", "content": "Hello"}, "finish_reason": "stop"}]
    assert body["usage"]["total_tokens"] == 7


def test_assemble_merges_tool_call_arguments_by_index():
    chunks = [
        {"id": "c", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                                                        "function": {"name": "get_weather", "arguments": ""}}]}}]},
        {"id": "c", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}}]},
        {"id": "c", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"Paris"}'}}]},
                                 "finish_reason": "tool_calls"}]},
    ]
    call = assemble(chunks)["choices"][0]["message"]["tool_calls"][0]
    assert call == {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}


def test_replay_round_trips_through_assemble():
    body = assemble(STREAM_CHUNKS)
    lines = replay(body, include_usage=True)
    assert lines[-1] == "data: [DONE]\n\n"
    chunks = [json.loads(l[len("data: "):]) for l in lines[:-1]]
    assert assemble(chunks)["choices"] == body["choices"]
    assert chunks[-1]["usage"] == body["usage"]


# --- through the gateway ---

def post(client, body=CHAT_BODY, headers=CACHE_ON):
    return client.post("/v1/chat/completions", json=body, headers=headers)


@respx.mock
def test_off_by_default(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    first, second = post(client, headers={}), post(client, headers={})

    assert route.call_count == 2
    assert first.headers["x-gateway-cache"] == second.headers["x-gateway-cache"] == "off"


@respx.mock
def test_second_identical_request_is_served_from_the_cache(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    first = post(client)
    wait_for(lambda: stored_keys(client))
    second = post(client)

    assert route.call_count == 1  # OpenAI was called once
    assert first.headers["x-gateway-cache"] == "miss"
    assert second.headers["x-gateway-cache"] == "hit"
    assert second.json() == first.json()
    assert second.headers["x-gateway-provider"] == "openai"
    assert int(second.headers["age"]) >= 0


@respx.mock
def test_team_default_turns_it_on(client, monkeypatch):
    monkeypatch.setitem(main.CONFIG.team_cache, "test-team", CacheSettings(enabled=True, ttl=600))
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    post(client, headers={})
    wait_for(lambda: stored_keys(client))
    assert post(client, headers={}).headers["x-gateway-cache"] == "hit"
    assert post(client, headers={"x-gateway-cache": "off"}).headers["x-gateway-cache"] == "off"  # per request: skip it
    assert route.call_count == 2
    key = stored_keys(client)[0]
    assert 0 < client.portal.call(main.app.state.cache.redis.ttl, key) <= 600


@respx.mock
def test_stream_hit_from_a_stored_non_stream_answer(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    post(client)
    wait_for(lambda: stored_keys(client))

    resp = post(client, {**CHAT_BODY, "stream": True, "stream_options": {"include_usage": True}})

    assert resp.headers["x-gateway-cache"] == "hit"
    assert resp.headers["content-type"].startswith("text/event-stream")
    lines = [l for l in resp.text.split("\n") if l.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(l[len("data: "):]) for l in lines[:-1]]
    assert assemble(chunks)["choices"][0]["message"]["content"] == "hello"
    assert chunks[-1]["usage"] == FAKE_COMPLETION["usage"]


FAKE_SSE = (
    b'data: {"id":"c1","created":1,"model":"gpt-4o-mini","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"}}]}\n\n'
    b'data: {"id":"c1","created":1,"model":"gpt-4o-mini","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
    b'data: {"id":"c1","created":1,"model":"gpt-4o-mini","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":1,"total_tokens":6}}\n\n'
    b"data: [DONE]\n\n"
)


@respx.mock
def test_streamed_answer_is_stored_and_serves_a_non_stream_request(client):
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, content=FAKE_SSE, headers={"content-type": "text/event-stream"}))
    post(client, {**CHAT_BODY, "stream": True}).read()
    wait_for(lambda: stored_keys(client))

    resp = post(client)

    assert route.call_count == 1
    assert resp.headers["x-gateway-cache"] == "hit"
    assert resp.json()["choices"][0]["message"]["content"] == "Hi"


@respx.mock
def test_cut_off_answers_are_not_stored(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json={
        **FAKE_COMPLETION, "choices": [{"index": 0, "message": {"role": "assistant", "content": "hel"}, "finish_reason": "length"}]}))
    post(client)
    time.sleep(0.1)
    assert stored_keys(client) == []
    assert post(client).headers["x-gateway-cache"] == "miss"


@respx.mock
def test_fallback_answers_are_not_stored(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(500))
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = post(client)

    assert resp.status_code == 200 and resp.headers["x-gateway-provider"] == "ollama"
    time.sleep(0.1)
    assert stored_keys(client) == []


@respx.mock
def test_errors_are_not_stored(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad"}}))
    post(client)
    time.sleep(0.1)
    assert stored_keys(client) == []


@respx.mock
def test_hit_costs_nothing_and_records_what_it_saved(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    post(client)
    wait_for(lambda: stored_keys(client))
    post(client)

    buffer = main.app.state.budgets.buffer
    wait_for(lambda: len(buffer) == 2)
    miss, hit = (dict(zip(budgets.COLUMNS, row)) for row in buffer)
    assert miss["cost_micros"] == 1 and not miss["cache_hit"]
    assert hit["cost_micros"] == 0 and hit["cache_hit"]
    assert hit["saved_micros"] == 1  # 5 x 0.15 + 1 x 0.60 = 1.35 micro-dollars, rounded
    assert hit["deployment"] == "openai/gpt-4o-mini"


@respx.mock
def test_hit_uses_a_request_but_refunds_its_tokens(client, monkeypatch):
    monkeypatch.setitem(main.CONFIG.team_limits, "test-team", main.ratelimit.Limits(rpm=100, tpm=100_000))
    monkeypatch.setattr(main.app.state.limiter, "clock", lambda: 1_000_000.0)  # no refill between requests
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    post(client)
    wait_for(lambda: stored_keys(client))
    hit = post(client)
    wait_for(lambda: len(main.app.state.budgets.buffer) == 2)  # the hit has been settled

    after = post(client)  # the next response shows where the buckets are now

    assert hit.headers["x-gateway-cache"] == "hit"
    assert after.headers["x-ratelimit-remaining-requests"] == "97"  # all three requests counted
    estimate = main.ratelimit.estimate_tokens(CHAT_BODY["messages"])
    # Tokens taken: the miss's real 6, the hit's estimate refunded, and the estimate for `after` (not settled yet).
    assert int(after.headers["x-ratelimit-remaining-tokens"]) == pytest.approx(100_000 - 6 - estimate, abs=1)


@respx.mock
def test_another_team_does_not_get_this_teams_answer(client, key_store):
    other_key = "sbk_" + "o" * 32
    key_store.by_hash[main.keys.hash_key(other_key)] = main.keys.KeyInfo(2, "other-team", other_key[:12])
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    post(client)
    wait_for(lambda: stored_keys(client))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY,
                       headers={**CACHE_ON, "Authorization": f"Bearer {other_key}"})

    assert resp.headers["x-gateway-cache"] == "miss"
    assert route.call_count == 2


@respx.mock
def test_cache_redis_down_means_every_request_is_a_miss(client, fake_cache_redis):
    fake_cache_redis.connected = False
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    first, second = post(client), post(client)

    assert first.status_code == second.status_code == 200
    assert route.call_count == 2
