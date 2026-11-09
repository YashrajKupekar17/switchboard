import asyncio

import fakeredis
import httpx
import pytest
import respx
from redis.exceptions import ConnectionError as RedisConnectionError

import main
from metrics import RATE_LIMIT_DEGRADED
from ratelimit import Limits, LocalBuckets, RateLimiter, estimate_tokens
from tests.conftest import TEST_KEY
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OPENAI_URL


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


def limiter(rpm=3, tpm=1000, copies=1):
    clock = Clock()
    return RateLimiter(fakeredis.FakeAsyncRedis(), lambda team: Limits(rpm, tpm), copies, clock), clock


def run(coro):
    return asyncio.run(coro)


def test_bucket_allows_a_burst_up_to_the_limit_then_refuses():
    async def go():
        rl, _ = limiter(rpm=3)
        results = [await rl.check("t", 10) for _ in range(4)]
        assert [d.allowed for d in results] == [True, True, True, False]
        assert results[3].exceeded == "requests"
        assert results[3].retry_after == pytest.approx(20)  # 3 per minute = one coin every 20 s
    run(go())


def test_bucket_refills_over_time_but_never_past_its_size():
    async def go():
        rl, clock = limiter(rpm=3)
        for _ in range(3):
            await rl.check("t", 10)
        clock.now += 20
        assert (await rl.check("t", 10)).allowed  # one coin came back
        assert not (await rl.check("t", 10)).allowed

        clock.now += 3600  # an hour idle earns 180 coins, but the bucket only holds 3
        results = [(await rl.check("t", 10)).allowed for _ in range(4)]
        assert results == [True, True, True, False]
    run(go())


def test_refused_request_takes_nothing_from_either_bucket():
    async def go():
        rl, _ = limiter(rpm=3, tpm=100)
        refused = await rl.check("t", 500)  # too many tokens
        assert not refused.allowed and refused.exceeded == "tokens"
        ok = await rl.check("t", 10)
        assert ok.remaining_requests == 2  # the refused request didn't spend a request coin
        assert ok.remaining_tokens == 90
    run(go())


def test_teams_have_separate_buckets():
    async def go():
        rl, _ = limiter(rpm=1)
        assert (await rl.check("search", 1)).allowed
        assert not (await rl.check("search", 1)).allowed
        assert (await rl.check("ads", 1)).allowed
    run(go())


def test_settle_charges_the_real_usage_and_can_go_into_debt():
    async def go():
        rl, clock = limiter(rpm=100, tpm=1000)
        assert (await rl.check("t", 100)).allowed  # estimate: 100, bucket 1000 -> 900
        await rl.settle("t", 100, {"total_tokens": 1500})  # the answer was long: charge 1400 more -> -500
        d = await rl.check("t", 10)
        assert not d.allowed and d.exceeded == "tokens"
        assert d.retry_after == pytest.approx(30.6)  # (10 - -500) tokens at 1000/60 per second

        clock.now += 31
        assert (await rl.check("t", 10)).allowed  # debt paid off by refills
    run(go())


def test_settle_refunds_an_overestimate():
    async def go():
        rl, _ = limiter(tpm=1000)
        await rl.check("t", 400)  # -> 600
        await rl.settle("t", 400, {"total_tokens": 100})  # refund 300 -> 900
        assert (await rl.check("t", 0)).remaining_tokens == 900
    run(go())


def test_settle_without_usage_keeps_the_estimate():
    async def go():
        rl, _ = limiter(tpm=1000)
        await rl.check("t", 400)
        await rl.settle("t", 400, None)  # stream cut short: no usage report
        assert (await rl.check("t", 0)).remaining_tokens == 600
    run(go())


def test_redis_script_and_local_fallback_agree():
    # Same sequence of takes against both. The local version only runs during Redis outages,
    # so it needs this test to stay honest.
    steps = [(0, 1, 50, False), (1, 1, 300, False), (2, 1, 900, False), (5, 0, 400, True), (5, 1, 10, False),
             (40, 1, 10, False), (200, 1, 10, False)]

    async def go():
        client = fakeredis.FakeAsyncRedis()
        script = client.register_script(main.ratelimit.TAKE)
        local = LocalBuckets()
        keys = ["rl:t:rpm", "rl:t:tpm"]
        for t, requests, tokens, force in steps:
            now = 1_000_000 + t
            r = await script(keys=keys, args=[now, 3, 1000, requests, tokens, int(force)])
            from_redis = [int(r[0]), float(r[1]), float(r[2]), float(r[3]), int(r[4])]
            from_local = local.take(keys, now, [3, 1000], [requests, tokens], force)
            assert from_redis == pytest.approx(from_local)
    run(go())


def test_redis_down_fails_open_to_a_per_copy_limit():
    async def go():
        rl, clock = limiter(rpm=4, copies=2)

        async def broken(**kwargs):
            raise RedisConnectionError("connection refused")

        working = rl.script
        rl.script = broken
        results = [(await rl.check("t", 1)).allowed for _ in range(3)]
        assert results == [True, True, False]  # 4 per minute split across 2 copies = 2 here
        assert RATE_LIMIT_DEGRADED._value.get() == 1

        rl.script = working
        clock.now += 5  # after the retry interval, Redis is tried again
        assert (await rl.check("t", 1)).allowed
        assert RATE_LIMIT_DEGRADED._value.get() == 0
    run(go())


def test_estimate_is_about_four_characters_per_token():
    assert estimate_tokens([{"role": "user", "content": "x" * 400}]) == 104  # 100 + 4 per message


# --- through the gateway ---

@pytest.fixture
def tight_limits(monkeypatch):
    monkeypatch.setitem(main.CONFIG.team_limits, "test-team", Limits(rpm=2, tpm=1000))


@respx.mock
def test_over_the_limit_gets_openai_style_429_without_calling_the_provider(client, tight_limits):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    assert client.post("/v1/chat/completions", json=CHAT_BODY).status_code == 200
    assert client.post("/v1/chat/completions", json=CHAT_BODY).status_code == 200
    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "rate_limit_exceeded"
    assert resp.json()["error"]["type"] == "requests"
    assert resp.headers["Retry-After"] == "30"  # 2 per minute = one every 30 s
    assert resp.headers["x-ratelimit-remaining-requests"] == "0"
    assert route.call_count == 2


@respx.mock
def test_success_carries_rate_limit_headers_with_real_usage_settled(client, tight_limits):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))  # total_tokens: 6

    first = client.post("/v1/chat/completions", json=CHAT_BODY)
    assert first.headers["x-ratelimit-limit-requests"] == "2"
    assert first.headers["x-ratelimit-remaining-requests"] == "1"
    estimate = estimate_tokens(CHAT_BODY["messages"])
    assert first.headers["x-ratelimit-remaining-tokens"] == str(1000 - estimate)

    second = client.post("/v1/chat/completions", json=CHAT_BODY)
    # The first request was settled at its real 6 tokens; the second has only its estimate taken so far.
    assert int(second.headers["x-ratelimit-remaining-tokens"]) == pytest.approx(1000 - 6 - estimate, abs=1)


@respx.mock
def test_prompt_bigger_than_the_whole_limit_is_called_too_large(client, tight_limits):
    route = respx.post(OPENAI_URL)
    body = {**CHAT_BODY, "messages": [{"role": "user", "content": "x" * 8000}]}

    resp = client.post("/v1/chat/completions", json=body)

    assert resp.status_code == 429
    assert "too large" in resp.json()["error"]["message"]
    assert not route.called


@respx.mock
def test_openai_sdk_raises_rate_limit_error(client, tight_limits):
    from openai import OpenAI, RateLimitError

    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    sdk = OpenAI(base_url="http://testserver/v1", api_key=TEST_KEY, http_client=client, max_retries=0)
    sdk.chat.completions.create(**CHAT_BODY)
    sdk.chat.completions.create(**CHAT_BODY)
    with pytest.raises(RateLimitError):
        sdk.chat.completions.create(**CHAT_BODY)
