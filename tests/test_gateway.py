import json
import os

import httpx
import pytest
import respx

from tests.conftest import TEST_KEY

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
OLLAMA_URL = "http://ollama.test/v1/chat/completions"

CHAT_BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

FAKE_COMPLETION = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "model": "gpt-4o-mini",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello"}, "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
}


@respx.mock
def test_forwards_request_and_returns_openai_response(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "hello"
    sent = route.calls.last.request
    assert sent.headers["Authorization"] == f"Bearer {os.environ['OPENAI_API_KEY']}"
    assert b"temperature" not in sent.content  # unset optional fields are not sent


@respx.mock
def test_client_error_is_returned_as_is_without_fallback(client):
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "temperature must be <= 2"}})
    )
    ollama = respx.post(OLLAMA_URL)

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "temperature": 5})

    assert resp.status_code == 400  # the request is broken everywhere; another provider can't fix it
    assert resp.json()["error"]["message"] == "temperature must be <= 2"
    assert not ollama.called


@respx.mock
def test_invalid_request_is_rejected_without_calling_openai(client):
    route = respx.post(OPENAI_URL)

    resp = client.post("/v1/chat/completions", json={"messages": []})  # no model

    assert resp.status_code == 422
    assert not route.called


FAKE_SSE = (
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":""}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"content":"Hel"}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
    b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2,"total_tokens":7}}\n\n'
    b"data: [DONE]\n\n"
)


@respx.mock
def test_streams_chunks_through_unchanged(client):
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, content=FAKE_SSE, headers={"content-type": "text/event-stream"})
    )

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert resp.content == FAKE_SSE
    sent = json.loads(route.calls.last.request.content)
    assert sent["stream_options"]["include_usage"] is True  # gateway asks for usage so it can track cost


@respx.mock
def test_stream_client_error_before_first_chunk_returns_error_status(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "bad request"}}))

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})

    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "bad request"


@respx.mock
def test_unknown_openai_fields_are_forwarded(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    client.post("/v1/chat/completions", json={**CHAT_BODY, "top_p": 0.5})

    assert json.loads(route.calls.last.request.content)["top_p"] == 0.5


class BreaksMidStream(httpx.AsyncByteStream):
    """Fake provider stream: sends one chunk, then the connection dies."""

    async def __aiter__(self):
        yield b'data: {"choices":[{"index":0,"delta":{"content":"Our refund policy"}}]}\n\n'
        raise httpx.ReadError("connection reset by provider")


@respx.mock
def test_mid_stream_failure_sends_error_event(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, stream=BreaksMidStream()))

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "stream": True})

    body = resp.text
    assert "Our refund policy" in body  # the part already sent stays sent
    assert '"error"' in body
    assert "[DONE]" not in body  # never claim the stream finished normally


@respx.mock
def test_openai_sdk_raises_on_mid_stream_failure(client):
    """End to end: a real OpenAI SDK client sees an exception, not a silently truncated answer."""
    from openai import APIError, OpenAI

    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, stream=BreaksMidStream()))
    respx.route(host="testserver").pass_through()  # let the SDK reach our app via TestClient
    sdk = OpenAI(base_url="http://testserver/v1", api_key=TEST_KEY, http_client=client)

    received = []
    with pytest.raises(APIError, match="mid-stream"):
        for chunk in sdk.chat.completions.create(stream=True, **CHAT_BODY):
            received.append(chunk.choices[0].delta.content)

    assert received == ["Our refund policy"]


# --- Phase 2a: config-driven routing ---


@respx.mock
def test_routes_model_to_configured_provider_with_its_model_name(client):
    route = respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "local-small"})

    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "ollama"
    sent = route.calls.last.request
    assert json.loads(sent.content)["model"] == "llama3.2"  # translated to the provider's own name
    assert "Authorization" not in sent.headers  # Ollama has no key, so none is sent


@respx.mock
def test_unknown_model_is_rejected_without_calling_any_provider(client):
    openai, ollama = respx.post(OPENAI_URL), respx.post(OLLAMA_URL)

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "gpt-9-ultra"})

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "model_not_found"
    assert not openai.called and not ollama.called
