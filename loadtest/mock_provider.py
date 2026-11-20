"""A fake OpenAI for load tests (DECISIONS.md Q). It answers /v1/chat/completions after a delay we choose,
so a test costs nothing and every millisecond above that delay is the gateway's.

Set by env vars, read once at startup:
  MOCK_TTFT_MS      time before the first token (default 300)
  MOCK_TOKENS       output tokens per answer (default 40)
  MOCK_TOKENS_PER_S how fast the rest of the tokens come (default 50)
  MOCK_ERROR_RATE   share of requests answered with a 500 (default 0)

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

GAP = 1 / TOKENS_PER_S  # between tokens after the first
TOTAL = TTFT + (TOKENS - 1) * GAP


def _usage(messages: list) -> dict:
    prompt = sum(len(str(m.get("content") or "")) for m in messages) // 4 + 4 * len(messages)
    return {"prompt_tokens": prompt, "completion_tokens": TOKENS, "total_tokens": prompt + TOKENS}


def _chunk(id_: str, model: str, delta: dict, finish=None) -> str:
    body = {"id": id_, "object": "chat.completion.chunk", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    return f"data: {json.dumps(body)}\n\n"


async def chat(request: Request):
    payload = await request.json()
    if random.random() < ERROR_RATE:
        return JSONResponse({"error": {"message": "mock failure", "type": "server_error"}}, status_code=500)

    model = payload.get("model", "mock")
    id_ = f"chatcmpl-mock{random.getrandbits(32):08x}"
    usage = _usage(payload.get("messages", []))

    if not payload.get("stream"):
        await asyncio.sleep(TOTAL)
        return JSONResponse({
            "id": id_, "object": "chat.completion", "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "tok " * TOKENS},
                         "finish_reason": "stop"}],
            "usage": usage,
        })

    # Wait before sending the headers, like the gateway does (it holds them until the first token is in): then
    # time-to-first-byte means the same thing in the baseline and through the gateway.
    await asyncio.sleep(TTFT)

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
