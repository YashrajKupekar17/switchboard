"""Translate between OpenAI chat completions and Anthropic's Messages API. Decisions: DECISIONS.md section I.

Clients only ever speak OpenAI's format. When a target's provider has `type: anthropic`, the request is
translated on the way out, and the response (or stream) is translated back, so routing, fallback, the
breaker and metrics don't know Anthropic exists.
"""
import json
import time
from typing import AsyncIterator, List, Tuple

DEFAULT_MAX_TOKENS = 16000  # required by Anthropic; thinking tokens count toward it, so don't lowball

# Fields we know how to translate, or can safely ignore.
TRANSLATED = {"model", "messages", "max_tokens", "max_completion_tokens", "stop", "stream", "user", "stream_options"}
# Current Claude models only accept the default sampling, so sending these would get a 400. We drop them.
DROPPED = {"temperature", "top_p"}

STOP_REASONS = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "refusal": "content_filter"}


class Unsupported(Exception):
    """The request uses something Anthropic can't do (tools, n>1, ...). Not the client's fault, not Anthropic's."""


class StreamError(Exception):
    """Anthropic reported an error inside a 200 stream (e.g. overloaded_error), or the stream ended early."""

    def __init__(self, kind: str, detail: str):
        super().__init__(detail)
        self.kind = kind


def to_anthropic(payload: dict) -> Tuple[dict, List[str]]:
    """OpenAI request -> Anthropic request, plus the names of fields we dropped."""
    unknown = sorted(k for k in payload if k not in TRANSLATED | DROPPED and not (k == "n" and payload[k] == 1))
    if unknown:
        raise Unsupported(f"not supported by this provider: {', '.join(unknown)}")

    system, messages = [], []
    for m in payload["messages"]:
        if m["role"] in ("system", "developer"):
            system.append(m["content"])  # Anthropic takes the system prompt as a separate field
        elif m["role"] in ("user", "assistant"):
            messages.append({"role": m["role"], "content": m["content"]})
        else:
            raise Unsupported(f"message role {m['role']!r} not supported by this provider")

    body = {
        "model": payload["model"],
        "messages": messages,
        "max_tokens": payload.get("max_completion_tokens") or payload.get("max_tokens") or DEFAULT_MAX_TOKENS,
    }
    if system:
        body["system"] = "\n\n".join(system)
    if payload.get("stop"):
        stop = payload["stop"]
        body["stop_sequences"] = [stop] if isinstance(stop, str) else stop
    if payload.get("user"):
        body["metadata"] = {"user_id": payload["user"]}
    if payload.get("stream"):
        body["stream"] = True
    return body, sorted(k for k in payload if k in DROPPED)


def _usage(u: dict) -> dict:
    # OpenAI's prompt_tokens includes cached tokens; Anthropic reports cache reads and writes separately.
    prompt = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
    completion = u.get("output_tokens", 0)
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}


def from_anthropic(body: dict) -> dict:
    """Anthropic response -> OpenAI chat completion. Thinking blocks are left out; only the answer goes back."""
    text = "".join(b["text"] for b in body.get("content", []) if b.get("type") == "text")
    return {
        "id": body.get("id"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": body.get("model"),
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": STOP_REASONS.get(body.get("stop_reason"), "stop"),
        }],
        "usage": _usage(body.get("usage", {})),
    }


def error_to_openai(body: bytes) -> dict:
    """Anthropic error body -> OpenAI's shape, so clients only ever parse one error format."""
    try:
        err = json.loads(body).get("error", {})
    except ValueError:
        err = {}
    return {"error": {"message": err.get("message", "provider error"), "type": err.get("type", "api_error"), "code": None}}


def _chunk(msg: dict, delta: dict, finish_reason=None) -> str:
    chunk = {
        "id": msg["id"], "object": "chat.completion.chunk", "created": msg["created"], "model": msg["model"],
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return f"data: {json.dumps(chunk)}"


async def translate_stream(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Anthropic SSE events -> OpenAI SSE chunk lines (each followed by a blank line), ending with [DONE].

    Nothing is yielded until the model starts its first content block (thinking or text), and the gateway
    only commits to this provider at the first yielded line. So an overloaded_error right after
    message_start still falls back. Waiting for the first *text* instead would be too strict: with
    thinking on, text can start long after the first-chunk timeout.
    """
    msg, usage = None, {}
    started = False
    async for line in lines:
        if not line.startswith("data: "):
            continue  # "event: ..." lines and blank separators; the type is also in the JSON
        event = json.loads(line[len("data: "):])
        kind = event.get("type")

        if kind == "message_start":
            m = event["message"]
            msg = {"id": m.get("id"), "model": m.get("model"), "created": int(time.time())}
            usage = dict(m.get("usage", {}))
        elif kind == "error":
            err = event.get("error", {})
            failure = "server" if err.get("type") in ("overloaded_error", "api_error") else "network"
            raise StreamError(failure, f"{err.get('type')}: {err.get('message')}")
        elif msg is None:
            continue  # nothing is valid before message_start
        elif kind == "content_block_start" and not started:
            started = True
            yield _chunk(msg, {"role": "assistant", "content": ""})
            yield ""
        elif kind == "content_block_delta" and event["delta"].get("type") == "text_delta":
            yield _chunk(msg, {"content": event["delta"]["text"]})
            yield ""
        elif kind == "message_delta":
            usage.update(event.get("usage") or {})
            if not started:  # e.g. an empty answer: still open the message before finishing it
                started = True
                yield _chunk(msg, {"role": "assistant", "content": ""})
                yield ""
            yield _chunk(msg, {}, STOP_REASONS.get(event["delta"].get("stop_reason"), "stop"))
            yield ""
        elif kind == "message_stop":
            final = {"id": msg["id"], "object": "chat.completion.chunk", "created": msg["created"],
                     "model": msg["model"], "choices": [], "usage": _usage(usage)}
            yield f"data: {json.dumps(final)}"
            yield ""
            yield "data: [DONE]"
            yield ""
            return
        # ping, content_block_stop, thinking/signature deltas: nothing to send

    # The connection closed cleanly but the message never finished: the answer is incomplete.
    raise StreamError("network", "stream ended before message_stop")
