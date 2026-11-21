"""A fake OpenAI for load tests (DECISIONS.md Q). It answers /v1/chat/completions after a delay we choose,
so a test costs nothing and every millisecond above that delay is the gateway's.

Set by env vars, read once at startup:
  MOCK_TTFT_MS      time before the first token (default 300)
  MOCK_TOKENS       output tokens per answer (default 40)
  MOCK_TOKENS_PER_S how fast the rest of the tokens come (default 50)
  MOCK_ERROR_RATE   share of requests answered with a 500 (default 0)
  MOCK_CONTROL      optional JSON file, e.g. {"error_rate": 1} or {"ttft_ms": 20000}: overrides the two above while
                    it exists, so the chaos test (chaos.sh) can break the mock mid-run without restarting it

A non-streamed answer arrives all at once, after the time the stream would have taken.
Plain Starlette, not FastAPI: the mock must be much faster than the gateway, or we'd be measuring the mock.
"""
import asyncio
import json
import os
import random
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

TTFT = int(os.environ.get("MOCK_TTFT_MS", "300")) / 1000
TOKENS = int(os.environ.get("MOCK_TOKENS", "40"))
TOKENS_PER_S = float(os.environ.get("MOCK_TOKENS_PER_S", "50"))
ERROR_RATE = float(os.environ.get("MOCK_ERROR_RATE", "0"))

CONTROL = os.environ.get("MOCK_CONTROL")

GAP = 1 / TOKENS_PER_S  # between tokens after the first

_live = {"ttft": TTFT, "error_rate": ERROR_RATE, "read_at": 0.0}


def _settings():
    """TTFT and error rate right now. The control file is re-read at most twice a second. A file and not an HTTP
    endpoint, because each of the 4 worker processes has its own memory and an HTTP call would only reach one."""
    now = time.monotonic()
    if CONTROL and now - _live["read_at"] > 0.5:
        _live["read_at"] = now
        try:
            with open(CONTROL) as f:
                override = json.load(f)
        except (OSError, ValueError):
            override = {}  # no file (or half-written): back to normal
        _live["ttft"] = override.get("ttft_ms", TTFT * 1000) / 1000
        _live["error_rate"] = override.get("error_rate", ERROR_RATE)
    return _live["ttft"], _live["error_rate"]


def _usage(messages: list) -> dict:
    prompt = sum(len(str(m.get("content") or "")) for m in messages) // 4 + 4 * len(messages)
    return {"prompt_tokens": prompt, "completion_tokens": TOKENS, "total_tokens": prompt + TOKENS}


def _chunk(id_: str, model: str, delta: dict, finish=None) -> str:
    body = {"id": id_, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n"


async def chat(request: Request):
    payload = await request.json()
    ttft, error_rate = _settings()
    if random.random() < error_rate:
        return JSONResponse({"error": {"message": "mock failure", "type": "server_error"}}, status_code=500)

    model = payload.get("model", "mock")
    id_ = f"chatcmpl-mock{random.getrandbits(32):08x}"
    usage = _usage(payload.get("messages", []))

    if not payload.get("stream"):
        await asyncio.sleep(ttft + (TOKENS - 1) * GAP)
        return JSONResponse({
            "id": id_, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "tok " * TOKENS},
                         "finish_reason": "stop"}],
            "usage": usage,
        })

    # Wait before sending the headers, like the gateway does (it holds them until the first token is in): then
    # time-to-first-byte means the same thing in the baseline and through the gateway.
    await asyncio.sleep(ttft)

    async def events():
        yield _chunk(id_, model, {"role": "assistant", "content": "tok "})
        for _ in range(TOKENS - 1):
            await asyncio.sleep(GAP)
            yield _chunk(id_, model, {"content": "tok "})
        yield _chunk(id_, model, {}, finish="stop")
        if (payload.get("stream_options") or {}).get("include_usage"):
            body = {"id": id_, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
                    "choices": [], "usage": usage}
            yield f"data: {json.dumps(body)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


def health(request: Request):
    return JSONResponse({"ok": True})


app = Starlette(routes=[Route("/v1/chat/completions", chat, methods=["POST"]), Route("/healthz", health)])
