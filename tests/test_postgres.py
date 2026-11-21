"""The key store and ledger against a real Postgres: the SQL, the audit trail and the gateway user's permissions.

Skipped unless TEST_DATABASE_URL is set, and only runs against a database whose name ends in _test, because it
empties the tables. CI runs it against a Postgres service; locally:

  docker compose exec postgres createdb -U switchboard switchboard_test
  TEST_DATABASE_URL=postgresql://switchboard:<password>@localhost:5432/switchboard_test .venv/bin/python -m pytest tests/test_postgres.py
"""
import asyncio
import datetime as dt
import json
import os
from decimal import Decimal

import asyncpg
import pytest

import budgets
import keys
from scripts.setup_db import GRANTS

URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
if URL and not URL.rsplit("/", 1)[-1].split("?")[0].endswith("_test"):
    raise RuntimeError("TEST_DATABASE_URL must name a database ending in _test: these tests empty its tables")


def run(test):
    async def wrapped():
        conn = await asyncpg.connect(URL)
        await conn.execute(keys.SCHEMA)
        await conn.execute(budgets.SCHEMA)
        await conn.execute(GRANTS)
        await conn.execute("TRUNCATE key_audit, usage_ledger, api_keys RESTART IDENTITY CASCADE")
        await conn.close()
        store = keys.PostgresKeys(await asyncpg.create_pool(URL, min_size=1, max_size=2))
        try:
            await test(store)
        finally:
            await store.close()
    asyncio.run(wrapped())


async def audit(store):
    return [(r["action"], r["key_id"], json.loads(r["details"])) for r in reversed(await store.audit())]


def test_create_stores_only_the_hash_and_audits_in_the_same_transaction():
    async def test(store):
        info, key = await store.create("search", "prod", actor="asha", rpm=300, max_usd=Decimal("200"))

        row = await store.pool.fetchrow("SELECT * FROM api_keys WHERE id = $1", info.id)
        assert row["key_hash"] == keys.hash_key(key) and key not in json.dumps(dict(row), default=str)
        assert await store.find(keys.hash_key(key)) == info
        assert (info.rpm, info.tpm, info.max_usd) == (300, None, Decimal("200"))
        (action, key_id, details), = await audit(store)
        assert (action, key_id, details["team"], details["rpm"]) == ("create", info.id, "search", 300)
        assert (await store.audit())[0]["actor"] == "asha"
    run(test)


def test_revoked_keys_are_not_found_and_expired_ones_are_but_say_so():
    async def test(store):
        revoked, revoked_key = await store.create("t")
        await store.revoke(revoked.id, actor="asha", reason="leaked")
        past = dt.datetime(2020, 1, 1, tzinfo=dt.timezone.utc)
        expired, expired_key = await store.create("t", expires_at=past)

        assert await store.find(keys.hash_key(revoked_key)) is None
        found = await store.find(keys.hash_key(expired_key))
        assert found.expired(dt.datetime.now(dt.timezone.utc))  # so the gateway can answer "expired", not "invalid"
        assert not await store.revoke(revoked.id)  # already revoked: no second audit row
        assert [a for a, _, _ in await audit(store)] == ["create", "revoke", "create"]
    run(test)


def test_rotation_copies_settings_and_gives_the_old_key_a_grace_period():
    async def test(store):
        old, old_key = await store.create("search", "prod", tpm=5000, max_usd=Decimal("50"))
        new, new_key = await store.rotate(old.id, dt.timedelta(hours=24), actor="asha")

        assert (new.team, new.name, new.tpm, new.max_usd) == ("search", "prod", 5000, Decimal("50"))
        assert new_key != old_key
        still = await store.find(keys.hash_key(old_key))
        hours_left = (still.expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds() / 3600
        assert 23.9 < hours_left <= 24  # both keys work during the grace period
        rotated_from = await store.pool.fetchval("SELECT rotated_from FROM api_keys WHERE id = $1", new.id)
        assert rotated_from == old.id
        assert [a for a, _, _ in await audit(store)] == ["create", "create", "rotate"]
    run(test)


def test_a_rotated_key_gets_the_old_ones_lifetime_from_now():
    async def test(store):
        old, _ = await store.create("t", expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90))
        await store.pool.execute("UPDATE api_keys SET created_at = now() - interval '85 days' WHERE id = $1", old.id)
        new, _ = await store.rotate(old.id, dt.timedelta(hours=1))  # 5 days left on a 175-day key

        days = (new.expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds() / 86400
        assert 174.9 < days <= 175
    run(test)


def test_sub_cent_budgets_are_stored_as_given():
    async def test(store):
        info, key = await store.create("t", max_usd=Decimal("0.000001"))
        assert (await store.find(keys.hash_key(key))).max_usd == Decimal("0.000001")
    run(test)


def test_rotation_never_extends_a_key_that_already_expires_sooner():
    async def test(store):
        soon = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        old, old_key = await store.create("t", expires_at=soon)
        await store.rotate(old.id, dt.timedelta(days=7))
        assert (await store.find(keys.hash_key(old_key))).expires_at == soon
    run(test)


def test_rotating_a_revoked_key_is_refused():
    async def test(store):
        old, _ = await store.create("t")
        await store.revoke(old.id)
        with pytest.raises(LookupError):
            await store.rotate(old.id, dt.timedelta(hours=1))
    run(test)


def test_update_sets_and_clears_settings_and_refuses_anything_else():
    async def test(store):
        info, key = await store.create("t", rpm=10)
        assert await store.update(info.id, actor="asha", rpm=None, max_usd=Decimal("5"))
        found = await store.find(keys.hash_key(key))
        assert (found.rpm, found.max_usd) == (None, Decimal("5"))
        with pytest.raises(ValueError):
            await store.update(info.id, team="other")  # a key never moves between teams
        await store.revoke(info.id)
        assert not await store.update(info.id, rpm=5)
        assert [a for a, _, _ in await audit(store)] == ["create", "update", "revoke"]
    run(test)


def test_last_used_is_one_batched_update_that_never_goes_backwards():
    async def test(store):
        a, _ = await store.create("t")
        b, _ = await store.create("t")
        t1 = dt.datetime(2026, 11, 20, 12, 0, tzinfo=dt.timezone.utc)
        t2 = t1 + dt.timedelta(minutes=5)

        await store.touch({a.id: t2, b.id: t1})
        await store.touch({a.id: t1})  # a late flush from another gateway copy, with an older time

        used = dict(await store.pool.fetch("SELECT id, last_used_at FROM api_keys"))
        assert used == {a.id: t2, b.id: t1}
    run(test)


def test_ledger_totals_per_key():
    async def test(store):
        ledger = budgets.PostgresLedger(store.pool)
        now = dt.datetime.now(dt.timezone.utc)
        row = lambda key_id, cost: (now, "t", key_id, "m", "d", 0, 0, 0, 0, cost, False, False, 0, None)
        k1, _ = await store.create("t")
        k2, _ = await store.create("t")
        await ledger.insert([row(k1.id, 300), row(k1.id, 200), row(k2.id, 1000)])

        since = now - dt.timedelta(minutes=1)
        assert await ledger.key_month_total(k1.id, since) == 500
        assert await ledger.month_total("t", since) == 1500
    run(test)


def test_the_gateway_user_can_touch_last_used_and_nothing_else():
    async def test(store):
        info, _ = await store.create("t")
        async with store.pool.acquire() as conn:
            await conn.execute("SET ROLE gateway")
            try:
                await conn.execute("UPDATE api_keys SET last_used_at = now() WHERE id = $1", info.id)
                for forbidden in ("UPDATE api_keys SET team = 'x'", "UPDATE api_keys SET revoked_at = NULL",
                                  "INSERT INTO key_audit (actor, action, key_id) VALUES ('g', 'create', 1)",
                                  "SELECT * FROM key_audit"):
                    with pytest.raises(asyncpg.InsufficientPrivilegeError):
                        await conn.execute(forbidden)
            finally:
                await conn.execute("RESET ROLE")
    run(test)
