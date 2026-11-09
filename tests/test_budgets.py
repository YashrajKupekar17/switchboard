import asyncio
import datetime as dt
import logging
import time

import fakeredis
import httpx
import pytest
import respx
from redis.exceptions import ConnectionError as RedisConnectionError

import budgets as budgets_mod
import main
from budgets import Budget, Budgets
from pricing import Tokens
from tests.conftest import FakeLedger
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OLLAMA_URL, OPENAI_URL

NOV = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)
NO_TOKENS = Tokens(0, 0, 0, 0)


def tracker(soft=1.0, max_=1.2, now=NOV):
    clock = {"now": now}
    ledger = FakeLedger()
    b = Budgets(fakeredis.FakeAsyncRedis(), ledger, lambda team: Budget(soft, max_), lambda: clock["now"])
    return b, ledger, clock


async def spend(b, micros, team="t"):
    await b.record(team=team, key_id=1, model="m", deployment="d", tokens=NO_TOKENS,
                   cost_micros=micros, estimated=False)


def run(coro):
    return asyncio.run(coro)


def test_under_budget_is_allowed_and_spend_adds_up():
    async def go():
        b, _, _ = tracker()
        assert (await b.check("t")).spent_micros == 0  # first check builds the total (from an empty ledger)
        await spend(b, 300_000)
        await spend(b, 200_000)
        s = await b.check("t")
        assert s.allowed and s.spent_micros == 500_000
    run(go())


def test_at_max_requests_are_refused():
    async def go():
        b, _, _ = tracker(soft=1.0, max_=1.2)
        await b.check("t")
        await spend(b, 1_100_000)
        assert (await b.check("t")).allowed  # past soft: still allowed
        await spend(b, 100_000)
        assert not (await b.check("t")).allowed  # at max: refused
    run(go())


def test_soft_budget_warns_once_per_month(caplog):
    async def go():
        b, _, _ = tracker(soft=1.0, max_=2.0)
        await b.check("t")
        await spend(b, 1_000_000)
        with caplog.at_level(logging.WARNING, logger="gateway"):
            for _ in range(3):
                await b.check("t")
        assert sum("soft budget" in r.message for r in caplog.records) == 1
    run(go())


def test_missing_total_is_rebuilt_from_the_ledger():
    async def go():
        b, ledger, _ = tracker()
        await spend(b, 400_000)  # no total in Redis yet, so this only goes to the ledger buffer
        await b.flush()
        assert len(ledger.rows) == 1
        assert (await b.check("t")).spent_micros == 400_000  # rebuilt with a SUM over this month's rows
        assert 0 < await b.redis.ttl("spend:t:2026-11") <= budgets_mod.TOTAL_TTL  # and rebuilt again later
    run(go())


def test_new_month_starts_at_zero():
    async def go():
        b, _, clock = tracker()
        await b.check("t")
        await spend(b, 900_000)
        await b.flush()
        clock["now"] = dt.datetime(2026, 12, 1, 0, 0, 1, tzinfo=dt.timezone.utc)
        assert (await b.check("t")).spent_micros == 0  # November's rows aren't counted in December
    run(go())


def test_teams_are_separate():
    async def go():
        b, _, _ = tracker(soft=1.0, max_=1.0)
        await b.check("a")
        await spend(b, 1_000_000, team="a")
        assert not (await b.check("a")).allowed
        assert (await b.check("b")).allowed
    run(go())


def test_redis_down_skips_the_check_but_still_records():
    async def go():
        b, ledger, _ = tracker()

        async def broken(*args, **kwargs):
            raise RedisConnectionError("connection refused")

        b.redis.get = broken
        s = await b.check("t")
        assert s.allowed and s.spent_micros is None  # fail open
        await spend(b, 100_000)
        await b.flush()
        assert ledger.rows[0][9] == 100_000  # nothing lost: the ledger is the source of truth
    run(go())


def test_postgres_down_keeps_rows_and_retries():
    async def go():
        b, ledger, _ = tracker()
        ledger.down = True
        await spend(b, 1)
        await spend(b, 2)
        await b.flush()
        assert len(b.buffer) == 2 and ledger.rows == []
        ledger.down = False
        await b.flush()
        assert [r[9] for r in ledger.rows] == [1, 2] and b.buffer == []
    run(go())


def test_buffer_has_a_cap(monkeypatch):
    monkeypatch.setattr(budgets_mod, "MAX_BUFFER", 3)

    async def go():
        b, _, _ = tracker()
        for micros in range(5):
            await spend(b, micros)
        assert [r[9] for r in b.buffer] == [2, 3, 4]  # the oldest rows go first
    run(go())


# --- through the gateway ---

def wait_for(condition, timeout=2.0):
    # Usage is recorded by a background task after the response is sent.
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


@respx.mock
def test_request_is_priced_by_the_model_that_answered(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(500))
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 200
    buffer = main.app.state.budgets.buffer
    wait_for(lambda: buffer)
    row = dict(zip(budgets_mod.COLUMNS, buffer[0]))
    assert row["deployment"] == "ollama/llama3.2" and row["model"] == "gpt-4o-mini"
    assert row["cost_micros"] == 0  # ollama is free; openai would have charged
    assert row["team"] == "test-team" and row["key_id"] == 1


@respx.mock
def test_success_carries_budget_headers(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.headers["x-gateway-budget-spent-usd"] == "0.000000"
    assert resp.headers["x-gateway-budget-max-usd"] == "1000.00"


@respx.mock
def test_over_budget_gets_429_without_calling_the_provider(client, monkeypatch):
    monkeypatch.setitem(main.CONFIG.team_budgets, "test-team", Budget(soft_usd=0.000001, max_usd=0.000001))
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    assert client.post("/v1/chat/completions", json=CHAT_BODY).status_code == 200  # costs 1.35 µ$ -> 1
    wait_for(lambda: main.app.state.budgets.buffer)
    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "budget_exceeded"
    assert resp.json()["error"]["type"] == "insufficient_quota"
    assert int(resp.headers["Retry-After"]) > 0  # seconds until the 1st of next month
    assert route.call_count == 1


def test_spend_after_a_lost_total_does_not_hide_the_rest_of_the_month():
    async def go():
        b, ledger, _ = tracker(soft=5.0, max_=5.0)
        await b.check("t")
        await spend(b, 3_000_000)
        await b.flush()
        await b.redis.flushall()  # Redis restarted and lost the total
        await spend(b, 100)  # must not create a fresh total of 100 that forgets the $3 already spent
        await b.flush()
        assert (await b.check("t")).spent_micros == 3_000_100
    run(go())
