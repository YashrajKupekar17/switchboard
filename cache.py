"""Exact response cache. Decisions: DECISIONS.md section N.

Off unless a team turns it on (config.yaml, or the x-gateway-cache header per request): a cached answer to
"write a poem" at temperature 1 would quietly stop the team getting new poems.

The key is the team plus a hash of the whole request, minus the fields that only change how the answer is
delivered. Listing what to leave out (instead of what to put in) means a new OpenAI field automatically
becomes part of the key: a mistake in the key causes a miss, never a wrong answer.

Answers live in their own Redis (redis-cache, allkeys-lfu, no persistence), so memory pressure from the
cache can never evict rate-limit buckets. If that Redis is down, every lookup is a miss.
"""
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import redis.asyncio as redis
from redis.exceptions import RedisError

log = logging.getLogger("gateway")

NOT_IN_KEY = {"stream", "stream_options", "user"}  # how the answer is delivered or tagged, not what it says
STORABLE_FINISH = {"stop", "tool_calls"}  # complete answers only: not cut off ("length"), not filtered
MAX_TTL = 7 * 86400
REDIS_RETRY_AFTER = 5.0


@dataclass(frozen=True)
class CacheSettings:
    enabled: bool
    ttl: int  # seconds


@dataclass(frozen=True)
class Hit:
    body: dict  # the OpenAI-format chat.completion
    deployment: str  # who originally answered: "openai/gpt-4o-mini"
    age: int  # seconds since it was stored


def cache_key(team: str, payload: dict) -> str:
    request = {k: v for k, v in payload.items() if k not in NOT_IN_KEY}
    # Sorted keys and fixed separators, so {"a":1,"b":2} and {"b":2,"a":1} hash the same (canonicalisation).
    canonical = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return f"cache:{team}:{hashlib.sha256(canonical.encode()).hexdigest()}"


def storable(body: dict) -> bool:
    choices = body.get("choices") or []
    return bool(choices) and all(c.get("finish_reason") in STORABLE_FINISH for c in choices)


def settings_from(default: CacheSettings, header: Optional[str], ttl_header: Optional[str]) -> CacheSettings:
    """The team's default, overridden per request by x-gateway-cache: on|off and x-gateway-cache-ttl: seconds."""
    enabled = {"on": True, "off": False}.get((header or "").lower(), default.enabled)
    ttl = default.ttl
    if ttl_header and ttl_header.isdigit():
        ttl = min(max(1, int(ttl_header)), MAX_TTL)
    return CacheSettings(enabled, ttl)


class ResponseCache:
    def __init__(self, client: redis.Redis, clock=time.time):
        self.redis = client
        self.clock = clock
        self.down_until = 0.0

    async def get(self, key: str) -> Optional[Hit]:
        if time.monotonic() < self.down_until:
            return None
        try:
            raw = await self.redis.get(key)
        except (RedisError, OSError) as exc:
            self._failed(exc)
            return None
        if raw is None:
            return None
        entry = json.loads(raw)
        return Hit(entry["body"], entry["deployment"], max(0, int(self.clock() - entry["stored_at"])))

    async def put(self, key: str, body: dict, deployment: str, ttl: int) -> None:
        if time.monotonic() < self.down_until:
            return
        entry = {"body": body, "deployment": deployment, "stored_at": self.clock()}
        try:
            await self.redis.set(key, json.dumps(entry, separators=(",", ":")), ex=ttl)
        except (RedisError, OSError) as exc:
            self._failed(exc)

    def _failed(self, exc) -> None:
        if time.monotonic() >= self.down_until:
            log.error("cache: Redis unreachable (%r), every request is a miss until it's back", exc)
        self.down_until = time.monotonic() + REDIS_RETRY_AFTER

    async def close(self) -> None:
        await self.redis.aclose()


def assemble(chunks: List[dict]) -> dict:
    """Streamed chunks -> the chat.completion a non-streaming request would have got, so both can share an entry."""
    first = chunks[0]
    choices, usage = {}, None
    for chunk in chunks:
        if chunk.get("usage"):
            usage = chunk["usage"]
        for c in chunk.get("choices", []):
            choice = choices.setdefault(c["index"], {"content": "", "tool_calls": {}, "finish_reason": None})
            delta = c.get("delta", {})
            choice["content"] += delta.get("content") or ""
            for call in delta.get("tool_calls") or []:  # arguments arrive in pieces, matched by index
                merged = choice["tool_calls"].setdefault(call["index"], {"id": None, "type": "function",
                                                                         "function": {"name": "", "arguments": ""}})
                merged["id"] = call.get("id") or merged["id"]
                fn = call.get("function") or {}
                merged["function"]["name"] += fn.get("name") or ""
                merged["function"]["arguments"] += fn.get("arguments") or ""
            if c.get("finish_reason"):
                choice["finish_reason"] = c["finish_reason"]
    body = {
        "id": first.get("id"), "object": "chat.completion", "created": first.get("created"), "model": first.get("model"),
        "choices": [],
    }
    for index in sorted(choices):
        c = choices[index]
        message = {"role": "assistant", "content": c["content"] or None}
        if c["tool_calls"]:
            message["tool_calls"] = [c["tool_calls"][i] for i in sorted(c["tool_calls"])]
        body["choices"].append({"index": index, "message": message, "finish_reason": c["finish_reason"]})
    if usage:
        body["usage"] = usage
    return body


def replay(body: dict, include_usage: bool) -> List[str]:
    """A stored chat.completion -> SSE lines, for a streaming request that hit the cache."""
    base = {"id": body.get("id"), "object": "chat.completion.chunk", "created": body.get("created"),
            "model": body.get("model")}
    lines = []
    for choice in body["choices"]:
        message = choice["message"]
        delta = {"role": "assistant", "content": message.get("content") or ""}
        if message.get("tool_calls"):
            delta["tool_calls"] = [{**call, "index": i} for i, call in enumerate(message["tool_calls"])]
        lines.append({**base, "choices": [{"index": choice["index"], "delta": delta, "finish_reason": None}]})
        lines.append({**base, "choices": [{"index": choice["index"], "delta": {}, "finish_reason": choice["finish_reason"]}]})
    if include_usage and body.get("usage"):
        # The original numbers, same as a non-streamed hit returns. x-gateway-cache: hit says it cost nothing.
        lines.append({**base, "choices": [], "usage": body["usage"]})
    return [f"data: {json.dumps(chunk)}\n\n" for chunk in lines] + ["data: [DONE]\n\n"]
