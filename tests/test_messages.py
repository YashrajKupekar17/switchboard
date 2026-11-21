"""Requests in every shape OpenAI accepts reach the provider: a team only changes base_url (docs/USING.md)."""
import json

import httpx
import respx

from ratelimit import IMAGE_TOKENS, estimate_tokens
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OPENAI_URL

TOOL_CALL = {"id": "call_1", "type": "function", "function": {"name": "weather", "arguments": '{"city": "Pune"}'}}
IMAGE = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 1_000_000}}  # ~1 MB photo


@respx.mock
def test_tool_calling_across_turns_reaches_the_provider(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    messages = [
        {"role": "user", "content": "Weather in Pune?"},
        {"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]},  # the model asked for a tool
        {"role": "tool", "tool_call_id": "call_1", "content": '{"temp_c": 31}'},  # our answer to it
    ]

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "messages": messages})

    assert resp.status_code == 200
    sent = json.loads(route.calls.last.request.content)["messages"]
    assert sent[1]["tool_calls"] == [TOOL_CALL]
    assert sent[2] == messages[2]


@respx.mock
def test_image_parts_reach_the_provider_and_count_as_an_image_not_as_text(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    content = [{"type": "text", "text": "What's in this photo?"}, IMAGE]

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "messages": [{"role": "user", "content": content}]})

    assert resp.status_code == 200  # not a 429 "request too large": the default limit is 100,000 tokens a minute
    assert json.loads(route.calls.last.request.content)["messages"][0]["content"] == content


@respx.mock
def test_a_malformed_message_is_the_providers_to_judge(client):
    # The gateway checks only what it reads. OpenAI's own 400 reaches the client unchanged, and no fallback.
    error = {"error": {"message": "Invalid content type", "type": "invalid_request_error"}}
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(400, json=error))

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "messages": [{"role": "user", "content": [{"type": "x"}]}]})

    assert resp.status_code == 400
    assert resp.json() == error


def test_estimate_counts_text_parts_images_and_tool_calls():
    text_only = estimate_tokens([{"role": "user", "content": "x" * 400}])
    as_parts = estimate_tokens([{"role": "user", "content": [{"type": "text", "text": "x" * 400}]}])
    with_image = estimate_tokens([{"role": "user", "content": [{"type": "text", "text": "x" * 400}, IMAGE]}])
    tool_turn = estimate_tokens([{"role": "assistant", "content": None, "tool_calls": [TOOL_CALL]}])

    assert text_only == as_parts == 104  # 400 chars / 4 + 4 per message
    assert with_image == 104 + IMAGE_TOKENS  # not 250,000 for the base64
    assert tool_turn > 4  # the arguments count as input on the next turn
