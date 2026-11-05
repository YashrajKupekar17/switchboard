"""Phase 3a: team API keys (DECISIONS.md section J)."""
import asyncio

import httpx
import pytest
import respx

import keys
from keys import KeyCache, KeyInfo
from tests.conftest import FakeKeyStore
from tests.test_breaker import FakeClock
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OPENAI_URL

OTHER_KEY = "sbk_" + "o" * 32
UNKNOWN_KEY = "sbk_" + "u" * 32


def check(cache, key):
    return asyncio.run(cache.check(key))


@pytest.fixture
def cache_with_clock():
    clock = FakeClock()
    store = FakeKeyStore({OTHER_KEY: KeyInfo(2, "search", OTHER_KEY[:keys.VISIBLE_CHARS])})
    return KeyCache(store, clock=clock), store, clock


# --- keys themselves ---


def test_generated_keys_have_the_prefix_and_are_never_the_same():
    a, b = keys.generate_key(), keys.generate_key()
    assert a.startswith("sbk_") and len(a) == keys.KEY_LENGTH
    assert a != b


def test_only_the_hash_is_stored_and_it_reveals_nothing():
    key = keys.generate_key()
    assert keys.hash_key(key) == keys.hash_key(key)  # same key, same hash: that's how lookup works
    assert key[4:] not in keys.hash_key(key)
    assert keys.hash_key(keys.hash_key(key)) != keys.hash_key(key)  # sending a stolen hash doesn't work


# --- the cache (cache-aside, valid 60 s, invalid 10 s) ---


def test_valid_key_is_cached_for_60_seconds(cache_with_clock):
    cache, store, clock = cache_with_clock

    assert check(cache, OTHER_KEY).team == "search"
    clock.now += 59
    check(cache, OTHER_KEY)
    assert store.lookups == 1  # second check came from memory
    clock.now += 2
    check(cache, OTHER_KEY)
    assert store.lookups == 2


def test_invalid_key_is_cached_for_10_seconds(cache_with_clock):
    cache, store, clock = cache_with_clock

    assert check(cache, UNKNOWN_KEY) is None
    check(cache, UNKNOWN_KEY)
    assert store.lookups == 1  # a script retrying a bad key doesn't hammer Postgres
    clock.now += 11
    check(cache, UNKNOWN_KEY)
    assert store.lookups == 2


def test_revoked_key_stops_working_once_its_cache_entry_expires(cache_with_clock):
    cache, store, clock = cache_with_clock
    check(cache, OTHER_KEY)

    store.by_hash.clear()  # revoked in Postgres
    assert check(cache, OTHER_KEY) is not None  # still cached: up to 60 s of grace, by design
    clock.now += 61
    assert check(cache, OTHER_KEY) is None


def test_garbage_that_is_not_shaped_like_a_key_never_reaches_postgres(cache_with_clock):
    cache, store, _ = cache_with_clock
    for junk in ["", "hello", "sk-" + "x" * 40, "sbk_short"]:
        assert check(cache, junk) is None
    assert store.lookups == 0


def test_the_invalid_cache_cannot_grow_without_limit(cache_with_clock, monkeypatch):
    cache, _, _ = cache_with_clock
    monkeypatch.setattr(KeyCache, "MAX_INVALID", 5)
    for i in range(20):
        check(cache, "sbk_" + f"{i:032d}")
    assert len(cache._invalid) <= 5


def test_postgres_down_rejects_unknown_keys_but_cached_keys_keep_working(cache_with_clock):
    cache, store, _ = cache_with_clock
    check(cache, OTHER_KEY)

    store.down = True
    assert check(cache, OTHER_KEY).team == "search"
    with pytest.raises(keys.AuthUnavailable):
        check(cache, UNKNOWN_KEY)  # fail closed: we can't tell, so it doesn't get in


# --- through the gateway ---


@respx.mock
@pytest.mark.parametrize("header", [None, "", "Basic abc", "Bearer ", "sbk_" + "t" * 32])
def test_missing_or_malformed_header_is_401_and_never_reaches_a_provider(client, header):
    openai = respx.post(OPENAI_URL)
    headers = {"Authorization": header} if header is not None else {}
    client.headers.pop("Authorization")

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=headers)

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_api_key"
    assert not openai.called


@respx.mock
def test_unknown_key_is_401_in_openai_error_shape(client):
    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers={"Authorization": f"Bearer {UNKNOWN_KEY}"})

    assert resp.status_code == 401
    assert resp.json() == {"error": {
        "message": "invalid or revoked API key", "type": "invalid_request_error", "code": "invalid_api_key",
    }}


def test_auth_runs_before_the_body_is_validated(client):
    client.headers.pop("Authorization")

    resp = client.post("/v1/chat/completions", json={"not": "a chat request"})

    assert resp.status_code == 401  # strangers learn nothing about our request format


@respx.mock
def test_postgres_down_is_503_not_a_free_pass(client, key_store):
    key_store.down = True

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "auth_unavailable"


@respx.mock
def test_valid_key_reaches_the_provider(client, key_store):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    for _ in range(3):
        assert client.post("/v1/chat/completions", json=CHAT_BODY).status_code == 200

    assert key_store.lookups == 1  # one Postgres query, then memory


@respx.mock
def test_openai_sdk_raises_authentication_error_for_a_bad_key(client):
    from openai import AuthenticationError, OpenAI

    respx.route(host="testserver").pass_through()
    sdk = OpenAI(base_url="http://testserver/v1", api_key=UNKNOWN_KEY, http_client=client)

    with pytest.raises(AuthenticationError):
        sdk.chat.completions.create(**CHAT_BODY)


def test_health_and_metrics_do_not_need_a_key(client):
    client.headers.pop("Authorization")
    assert client.get("/healthz").status_code == 200
    assert client.get("/metrics").status_code == 200

