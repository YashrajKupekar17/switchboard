"""Shared test setup. pytest loads this before any test module."""
import os
from pathlib import Path

# Must be set before importing main, which loads config at import time.
os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")  # fake: tests never reach Anthropic
os.environ["GATEWAY_CONFIG"] = str(Path(__file__).parent / "config.test.yaml")

import fakeredis  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import keys  # noqa: E402
import main  # noqa: E402

TEST_KEY = "sbk_" + "t" * 32
AUTH = {"Authorization": f"Bearer {TEST_KEY}"}


class FakeKeyStore:
    """Stands in for Postgres: same find() and close(), backed by a dict. Counts lookups."""

    def __init__(self, active_keys: dict):
        self.by_hash = {keys.hash_key(k): info for k, info in active_keys.items()}
        self.lookups = 0
        self.down = False

    async def find(self, key_hash):
        self.lookups += 1
        if self.down:
            raise OSError("connection refused")
        return self.by_hash.get(key_hash)

    async def close(self):
        pass


class FakeLedger:
    """Stands in for the Postgres usage ledger: rows in a list."""

    def __init__(self):
        self.rows = []
        self.down = False

    async def insert(self, rows):
        if self.down:
            raise OSError("connection refused")
        self.rows.extend(rows)

    async def month_total(self, team, since):
        return sum(r[9] for r in self.rows if r[1] == team and r[0] >= since)  # r[9] = cost_micros


@pytest.fixture
def ledger(monkeypatch):
    fake = FakeLedger()

    async def open_fake(key_store):
        return fake

    monkeypatch.setattr(main, "open_ledger", open_fake)
    return fake


@pytest.fixture
def key_store(monkeypatch):
    store = FakeKeyStore({TEST_KEY: keys.KeyInfo(1, "test-team", TEST_KEY[:keys.VISIBLE_CHARS])})

    async def open_fake():
        return store

    monkeypatch.setattr(main, "open_key_store", open_fake)
    return store


@pytest.fixture
def fake_redis(monkeypatch):
    # An in-memory Redis that runs Lua, so the real rate limit script runs in tests.
    server = fakeredis.FakeServer()
    monkeypatch.setattr(main, "open_redis", lambda: fakeredis.FakeAsyncRedis(server=server))
    return server


@pytest.fixture
def client(key_store, fake_redis, ledger):
    # `with` runs lifespan, so the shared httpx client and the key cache exist. Every request is authenticated.
    with TestClient(main.app, headers=AUTH) as c:
        yield c
