"""A key's own limits, budget and expiry, inside its team's (DECISIONS.md T2, T3)."""
import asyncio
import datetime as dt
from decimal import Decimal

import fakeredis
import httpx
import respx

import budgets as budgets_mod
import keys
from budgets import Budget, Budgets
from keys import KeyInfo, LastUsed
from pricing import Tokens
from ratelimit import Limits, RateLimiter
from tests.conftest import FakeKeyStore, FakeLedger
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OPENAI_URL
from tests.test_ratelimit import Clock

SERVICE_KEY = "sbk_" + "s" * 32  # a second key in the same team, with its own settings
NOV = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)


def run(coro):
    return asyncio.run(coro)


def add_key(key_store, **settings):
    info = KeyInfo(2, "test-team", SERVICE_KEY[:keys.VISIBLE_CHARS], "batch job", **settings)
    key_store.by_hash[keys.hash_key(SERVICE_KEY)] = info
    return {"Authorization": f"Bearer {SERVICE_KEY}"}


# --- rate limits ---


def test_a_key_refused_by_its_own_limit_takes_nothing_from_its_team():
    async def go():
        rl = RateLimiter(fakeredis.FakeAsyncRedis(), lambda team: Limits(100, 10_000), clock=Clock())
        key = KeyInfo(2, "t", "sbk_x", rpm=1)
        assert (await rl.check("t", 10, key)).allowed
        refused = await rl.check("t", 10, key)
        team_only = await rl.check("t", 0)

        assert not refused.allowed and refused.exceeded.scope == "key" and refused.exceeded.kind == "requests"
        assert team_only.remaining_requests == 98  # 100 - the allowed request - this one: the refusal took nothing
    run(go())


@respx.mock
def test_key_limit_gives_a_429_naming_the_key_while_the_team_has_room(client, key_store):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    service = add_key(key_store, rpm=1)

    first = client.post("/v1/chat/completions", json=CHAT_BODY, headers=service)
    refused = client.post("/v1/chat/completions", json=CHAT_BODY, headers=service)
    other_key = client.post("/v1/chat/completions", json=CHAT_BODY)  # same team, the default test key

    assert first.headers["x-ratelimit-limit-requests"] == "1"  # the headers describe the tighter bucket
    assert refused.status_code == 429
    assert "key sbk_ssssssss (batch job)" in refused.json()["error"]["message"]
    assert other_key.status_code == 200


# --- budgets ---


def tracker(team_max=10.0):
    ledger = FakeLedger()
    b = Budgets(fakeredis.FakeAsyncRedis(), ledger, lambda team: Budget(team_max, team_max), lambda: NOV)
    return b, ledger


async def spend(b, micros, key_id):
    await b.record(team="t", key_id=key_id, model="m", deployment="d", tokens=Tokens(0, 0, 0, 0),
                   cost_micros=micros, estimated=False)


def test_a_key_over_its_own_budget_is_refused_while_its_team_has_room():
    async def go():
        b, _ = tracker()
        capped = KeyInfo(2, "t", "sbk_x", max_usd=Decimal("1.00"))
        uncapped = KeyInfo(3, "t", "sbk_y")
        assert (await b.check("t", capped)).key_spent_micros == 0  # builds the key's total
        await spend(b, 600_000, key_id=2)
        await spend(b, 500_000, key_id=2)

        over = await b.check("t", capped)
        assert not over.allowed and over.refused_by == "key" and over.key_spent_micros == 1_100_000
        assert (await b.check("t", uncapped)).allowed  # the team ($10) still has room
    run(go())


def test_a_key_total_is_rebuilt_from_the_ledger_by_key():
    async def go():
        b, _ = tracker()
        await spend(b, 300_000, key_id=2)  # before anyone checked: no running total yet
        await spend(b, 900_000, key_id=3)  # another key in the same team
        await b.flush()

        spend_now = await b.check("t", KeyInfo(2, "t", "sbk_x", max_usd=Decimal("1.00")))
        assert spend_now.key_spent_micros == 300_000 and spend_now.spent_micros == 1_200_000
    run(go())


def test_the_team_budget_still_wins_over_a_bigger_key_budget():
    async def go():
        b, _ = tracker(team_max=1.0)
        await b.check("t")
        await spend(b, 1_000_000, key_id=2)
        over = await b.check("t", KeyInfo(2, "t", "sbk_x", max_usd=Decimal("50")))
        assert not over.allowed and over.refused_by == "team"
    run(go())


@respx.mock
def test_key_budget_429_says_whose_budget_and_shows_both_in_headers(client, key_store):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    service = add_key(key_store, max_usd=Decimal("0"))  # a key with no budget at all

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=service)

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "budget_exceeded"
    assert "key sbk_ssssssss (batch job) has reached its own monthly budget of $0.00" in resp.json()["error"]["message"]
    assert resp.headers["x-gateway-key-budget-max-usd"] == "0.000000"


# --- expiry ---


@respx.mock
def test_an_expired_key_gets_its_own_error(client, key_store):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    service = add_key(key_store, expires_at=dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=service)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "expired_api_key"


@respx.mock
def test_a_cached_key_still_stops_at_its_expiry_time(client, key_store, monkeypatch):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    service = add_key(key_store, expires_at=NOV + dt.timedelta(seconds=30))
    monkeypatch.setattr(budgets_mod, "utcnow", lambda: NOV)
    assert client.post("/v1/chat/completions", json=CHAT_BODY, headers=service).status_code == 200

    monkeypatch.setattr(budgets_mod, "utcnow", lambda: NOV + dt.timedelta(seconds=31))  # still in the 60 s cache
    lookups = key_store.lookups
    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=service)

    assert key_store.lookups == lookups  # it came from the cache...
    assert resp.status_code == 401  # ...and was refused anyway


# --- last used ---


def test_last_used_is_written_in_one_batch_and_kept_if_the_write_fails():
    async def go():
        store = FakeKeyStore({})
        times = iter([NOV, NOV + dt.timedelta(minutes=1), NOV + dt.timedelta(minutes=2)])
        tracker = LastUsed(store, clock=lambda: next(times))
        tracker.saw(1)
        tracker.saw(1)  # only the latest time per key is kept
        store.down = True
        await tracker.flush()
        assert store.last_used == {} and tracker.seen == {1: NOV + dt.timedelta(minutes=1)}  # kept for later

        store.down = False
        tracker.saw(2)
        await tracker.flush()
        assert store.last_used == {1: NOV + dt.timedelta(minutes=1), 2: NOV + dt.timedelta(minutes=2)}
        assert tracker.seen == {}
    run(go())


def test_every_authenticated_request_is_noted_as_a_use(client):
    client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "made-up"})  # even one that fails later
    assert 1 in client.app.state.last_used.seen
