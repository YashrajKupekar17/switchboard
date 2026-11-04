"""Phase 2b: the retry / fallback policy, one test per decision we made."""
import asyncio

import httpx
import pytest
import respx

import upstream
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, FAKE_SSE, OLLAMA_URL, OPENAI_URL, client  # noqa: F401

OFF = {"x-gateway-fallback": "off"}


def ok():
    return httpx.Response(200, json=FAKE_COMPLETION)


# --- provider errors fall back to the next target ---


@pytest.mark.parametrize("status", [500, 502, 503])
@respx.mock
def test_5xx_falls_back_immediately(client, status):
    openai = respx.post(OPENAI_URL).mock(return_value=httpx.Response(status))
    respx.post(OLLAMA_URL).mock(return_value=ok())

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "ollama"
    assert resp.headers["x-gateway-attempts"] == "openai:server"
    assert openai.call_count == 1  # another target exists, so no retry on openai


@respx.mock
def test_401_is_our_problem_so_fall_back(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(401))
    respx.post(OLLAMA_URL).mock(return_value=ok())

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 200
    assert resp.headers["x-gateway-attempts"] == "openai:auth"


@respx.mock
def test_429_falls_back(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(429))
    respx.post(OLLAMA_URL).mock(return_value=ok())

    assert client.post("/v1/chat/completions", json=CHAT_BODY).headers["x-gateway-provider"] == "ollama"


@respx.mock
def test_connect_error_falls_back(client):
    respx.post(OPENAI_URL).mock(side_effect=httpx.ConnectError("refused"))
    respx.post(OLLAMA_URL).mock(return_value=ok())

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.headers["x-gateway-attempts"] == "openai:connect"


@respx.mock
def test_read_timeout_falls_back(client):
    respx.post(OPENAI_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    respx.post(OLLAMA_URL).mock(return_value=ok())

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.headers["x-gateway-attempts"] == "openai:timeout"


# --- when nothing is left: the final status codes we chose ---


@respx.mock
def test_opt_out_disables_fallback(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(401))
    ollama = respx.post(OLLAMA_URL)

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=OFF)

    assert resp.status_code == 502  # not 401: the client's credentials are fine, ours are not
    assert not ollama.called


@respx.mock
def test_rate_limited_with_nothing_left_returns_503_with_retry_after(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(429, headers={"retry-after": "7"}))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=OFF)

    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "7"


@respx.mock
def test_last_target_5xx_is_retried_once_then_succeeds(client):
    openai = respx.post(OPENAI_URL).mock(side_effect=[httpx.Response(503), ok()])

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=OFF)

    assert resp.status_code == 200
    assert openai.call_count == 2


@respx.mock
def test_last_target_5xx_twice_returns_502(client):
    openai = respx.post(OPENAI_URL).mock(return_value=httpx.Response(500))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=OFF)

    assert resp.status_code == 502
    assert openai.call_count == 2  # exactly one retry, never more
    assert resp.headers["x-gateway-attempts"] == "openai:server, openai:server"


@respx.mock
def test_read_timeout_is_never_retried(client):
    openai = respx.post(OPENAI_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers=OFF)

    assert resp.status_code == 504
    assert openai.call_count == 1  # the provider may still be working (and billing); don't pile on


@respx.mock
def test_every_provider_down_returns_502_listing_attempts(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(503))
    respx.post(OLLAMA_URL).mock(side_effect=httpx.ConnectError("refused"))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 502
    # openai fails once (fallback exists), ollama is last so its connect error is retried once
    assert resp.headers["x-gateway-attempts"] == "openai:server, ollama:connect, ollama:connect"
    assert [a["provider"] for a in resp.json()["error"]["attempts"]] == ["openai", "ollama", "ollama"]


# --- streaming: fallback is possible until the first chunk ---


def sse():
    return httpx.Response(200, content=FAKE_SSE, headers={"content-type": "text/event-stream"})


@respx.mock
def test_stream_falls_back_on_5xx_before_first_chunk(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(503))
    respx.post(OLLAMA_URL).mock(return_value=sse())

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})

    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "ollama"
    assert resp.content == FAKE_SSE


class SilentStream(httpx.AsyncByteStream):
    """Provider accepts the request (200) but never sends a chunk."""

    async def __aiter__(self):
        await asyncio.sleep(5)
        yield b""


@respx.mock
def test_stream_falls_back_when_first_chunk_is_too_slow(client, monkeypatch):
    monkeypatch.setattr(upstream, "FIRST_CHUNK_TIMEOUT", 0.2)
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, stream=SilentStream()))
    respx.post(OLLAMA_URL).mock(return_value=sse())

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})

    assert resp.status_code == 200  # the 200 was never sent for openai, so switching was still possible
    assert resp.headers["x-gateway-provider"] == "ollama"
    assert resp.headers["x-gateway-attempts"] == "openai:timeout"
