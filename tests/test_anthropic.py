"""Anthropic adapter: OpenAI-format clients talking to a fake Anthropic that follows its documented formats."""
import json

import httpx
import pytest
import respx

from anthropic_adapter import DEFAULT_MAX_TOKENS, Unsupported, to_anthropic
from tests.conftest import TEST_KEY
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OLLAMA_URL, OPENAI_URL
from tests.test_metrics import Delta

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

CLAUDE_BODY = {
    "model": "claude-small",
    "messages": [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}],
}

FAKE_MESSAGE = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},  # thinking is on by default; never sent back
        {"type": "text", "text": "Hello"},
        {"type": "text", "text": " there"},
    ],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 10, "output_tokens": 4, "cache_read_input_tokens": 5},
}

MESSAGE_START = {"type": "message_start", "message": {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-5-5", "content": [],
    "usage": {"input_tokens": 10, "output_tokens": 1},
}}
TEXT_START = {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}
OVERLOADED = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}


def text(t):
    return {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": t}}


FULL_STREAM = [
    MESSAGE_START,
    {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "ping"},
    TEXT_START, text("Hel"), text("lo"),
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
    {"type": "message_stop"},
]


def sse(events):
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


def anthropic_stream(events):
    return httpx.Response(200, content=sse(events), headers={"content-type": "text/event-stream"})


def data_lines(resp):
    return [line[len("data: "):] for line in resp.text.splitlines() if line.startswith("data: ")]


# --- request translation ---


def test_request_moves_system_prompt_and_fills_required_fields():
    body, dropped = to_anthropic({**CLAUDE_BODY, "model": "claude-haiku-5-5", "temperature": 0.2, "stop": "END"})

    assert body["system"] == "Be brief."
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == DEFAULT_MAX_TOKENS  # Anthropic requires it; OpenAI clients often omit it
    assert body["stop_sequences"] == ["END"]
    assert "temperature" not in body and dropped == ["temperature"]  # current Claude models reject it


def test_request_with_features_anthropic_cannot_do_is_unsupported():
    with pytest.raises(Unsupported, match="tools"):
        to_anthropic({**CLAUDE_BODY, "tools": [{"type": "function"}]})


# --- non-streaming ---


@respx.mock
def test_translates_request_and_response(client):
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=FAKE_MESSAGE))
    tokens = Delta("gateway_tokens_total", deployment="anthropic/claude-haiku-5-5", kind="prompt")

    resp = client.post("/v1/chat/completions", json={**CLAUDE_BODY, "temperature": 0.2})

    sent = route.calls.last.request
    assert sent.headers["x-api-key"] == "test-anthropic-key"
    assert sent.headers["anthropic-version"] == "2026-10-21"
    assert "authorization" not in sent.headers
    assert json.loads(sent.content)["system"] == "Be brief."

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "Hello there"}  # thinking left out
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["usage"] == {"prompt_tokens": 15, "completion_tokens": 4, "total_tokens": 19}  # cache reads count
    assert resp.headers["x-gateway-dropped-params"] == "temperature"
    assert tokens.change == 15


@respx.mock
def test_client_error_comes_back_in_openai_error_shape(client):
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(
        400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "messages: bad"}},
    ))

    resp = client.post("/v1/chat/completions", json=CLAUDE_BODY)

    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "messages: bad", "type": "invalid_request_error", "code": None}}


@respx.mock
def test_overloaded_529_falls_back(client):
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(529, json={"type": "error", "error": OVERLOADED["error"]}))
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json={**CLAUDE_BODY, "model": "claude-then-ollama"})

    assert resp.status_code == 200
    assert resp.headers["x-gateway-attempts"] == "anthropic:server"


@respx.mock
def test_unsupported_request_skips_anthropic_without_calling_it(client):
    anthropic = respx.post(ANTHROPIC_URL)
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json={**CLAUDE_BODY, "model": "claude-then-ollama", "tools": []})

    assert resp.status_code == 200
    assert resp.headers["x-gateway-attempts"] == "anthropic:unsupported"
    assert not anthropic.called
    assert client.app.state.breakers.get("anthropic", "claude-haiku-5-5").consecutive_failures == 0


def test_unsupported_with_nothing_else_left_is_a_400_naming_the_field(client):
    resp = client.post("/v1/chat/completions", json={**CLAUDE_BODY, "tools": []})

    assert resp.status_code == 400
    assert "tools" in resp.json()["error"]["message"]


@respx.mock
def test_a_real_provider_failure_outranks_unsupported(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(503))

    resp = client.post("/v1/chat/completions", json={**CLAUDE_BODY, "model": "openai-then-claude", "tools": []})

    assert resp.status_code == 502  # the client's request was fine; OpenAI was down


# --- streaming ---


@respx.mock
def test_stream_is_translated_into_openai_chunks(client):
    respx.post(ANTHROPIC_URL).mock(return_value=anthropic_stream(FULL_STREAM))

    resp = client.post("/v1/chat/completions", json={**CLAUDE_BODY, "stream": True})

    assert resp.status_code == 200
    lines = data_lines(resp)
    assert lines[-1] == "[DONE]"
    chunks = [json.loads(line) for line in lines[:-1]]
    assert all(c["object"] == "chat.completion.chunk" for c in chunks)
    content = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert content == "Hello"
    assert chunks[-2]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == {"prompt_tokens": 10, "completion_tokens": 7, "total_tokens": 17}


@respx.mock
def test_openai_sdk_streams_from_anthropic_unchanged(client):
    """End to end: the official OpenAI SDK reads a Claude stream as if it came from OpenAI."""
    from openai import OpenAI

    respx.post(ANTHROPIC_URL).mock(return_value=anthropic_stream(FULL_STREAM))
    respx.route(host="testserver").pass_through()
    sdk = OpenAI(base_url="http://testserver/v1", api_key=TEST_KEY, http_client=client)

    chunks = list(sdk.chat.completions.create(stream=True, **CLAUDE_BODY))

    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello"
    assert chunks[-1].usage.completion_tokens == 7


@respx.mock
def test_overloaded_inside_the_stream_before_any_content_still_falls_back(client):
    respx.post(ANTHROPIC_URL).mock(return_value=anthropic_stream([MESSAGE_START, {"type": "ping"}, OVERLOADED]))
    respx.post(OLLAMA_URL).mock(
        return_value=httpx.Response(200, content=b'data: {"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
                                    b"data: [DONE]\n\n", headers={"content-type": "text/event-stream"})
    )

    resp = client.post("/v1/chat/completions", json={**CLAUDE_BODY, "model": "claude-then-ollama", "stream": True})

    assert resp.status_code == 200
    assert resp.headers["x-gateway-provider"] == "ollama"
    assert resp.headers["x-gateway-attempts"] == "anthropic:server"


@respx.mock
@pytest.mark.parametrize("tail", [[OVERLOADED], []], ids=["error-event", "stream-just-ends"])
def test_failure_after_content_sends_error_event(client, tail):
    respx.post(ANTHROPIC_URL).mock(return_value=anthropic_stream([MESSAGE_START, TEXT_START, text("Hel")] + tail))
    mid_stream = Delta("gateway_upstream_attempts_total", deployment="anthropic/claude-haiku-5-5", outcome="mid_stream")

    resp = client.post("/v1/chat/completions", json={**CLAUDE_BODY, "stream": True})

    lines = data_lines(resp)
    assert "[DONE]" not in lines  # the answer is incomplete, and the client must know
    assert "error" in json.loads(lines[-1])
    assert mid_stream.change == 1
