"""Semantic cache: a question that means the same as a stored one gets the stored answer. Decisions: DECISIONS.md section O.

Runs after the exact cache misses, only for teams that turned it on (or to shadow mode, which looks but never serves):

    request -> partition (exact hash of everything except the last user message) + question (that message)
            -> embed the question (OpenAI, 500 ms timeout, circuit breaker)
            -> closest stored question in the same partition (Redis vector search)
            -> similarity >= the team's threshold ? hit : miss

Every failure on the way is a miss. The cache is there to save money; it must never be why a request fails or slows down.
"""
import hashlib
import json
import logging
import struct
import time
from dataclasses import dataclass
from typing import List, Optional, Tuple

import httpx
import redis.asyncio as redis
from redis.exceptions import RedisError, ResponseError

from breaker import CircuitBreaker
from cache import NOT_IN_KEY, Hit
from metrics import EMBEDDING_SECONDS

log = logging.getLogger("gateway")

MODES = ("off", "shadow", "on")
EMBED_MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
EMBED_TIMEOUT = 1.0  # just above the call's normal slowest, measured (O4): slower counts as a miss
MAX_QUESTION_CHARS = 2000  # in a longer prompt, one changed sentence still scores ~0.99 (O3)
INDEX = "semantic"
PREFIX = "sem:"  # every hash under this prefix is in the index
SHADOW_KEEP = 1000  # pairs kept per team, for picking a threshold
SHADOW_FLOOR = 0.70  # shadow keeps every closest match from here up, not just those over the threshold it's there to pick
SHADOW_TTL = 7 * 86400
REDIS_RETRY_AFTER = 5.0


@dataclass(frozen=True)
class Lookup:
    partition: str  # hash of everything that has to match exactly
    question: str  # the last user message, compared by meaning


@dataclass(frozen=True)
class Match:
    similarity: float  # cosine similarity, 1.0 = same direction
    question: str  # the stored question it matched
    key: str  # the stored entry, for logs: never log the question itself
    hit: Hit


def lookup_for(team: str, payload: dict) -> Optional[Lookup]:
    """None if this request can't use the semantic cache: the last message isn't a short user question."""
    messages = payload.get("messages") or []
    if not messages or messages[-1].get("role") != "user":
        return None
    question = messages[-1].get("content")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        return None
    rest = {k: v for k, v in payload.items() if k not in NOT_IN_KEY}
    # Everything but the question's text has to match exactly, including the last message's other fields.
    rest["messages"] = messages[:-1] + [{k: v for k, v in messages[-1].items() if k != "content"}]
    # The embedding model is in the partition: vectors from two models can't be compared (O3).
    canonical = json.dumps({"team": team, "embedder": EMBED_MODEL, "request": rest},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return Lookup(hashlib.sha256(canonical.encode()).hexdigest(), question)


class Embedder:
    """Question -> vector, via OpenAI's embeddings API. None means "couldn't, treat it as a miss"."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str, breaker: Optional[CircuitBreaker] = None):
        self.http = http
        self.url = f"{base_url.rstrip('/')}/embeddings"
        self.api_key = api_key
        # The same breaker as for providers: once the API is failing, stop paying the timeout on every request.
        self.breaker = breaker or CircuitBreaker(f"embeddings/{EMBED_MODEL}", failure_threshold=3)

    async def embed(self, text: str) -> Optional[List[float]]:
        permit = self.breaker.allow()
        if permit is None:
            return None
        outcome = None
        start = time.monotonic()
        try:
            response = await self.http.post(
                self.url, json={"model": EMBED_MODEL, "input": text},
                headers={"Authorization": f"Bearer {self.api_key}"}, timeout=EMBED_TIMEOUT,
            )
            response.raise_for_status()
            vector = response.json()["data"][0]["embedding"]
            outcome = "ok"
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            outcome = "failed"
            self.breaker.record_failure(permit)
            log.warning("embedding failed (%r): semantic lookup skipped, request continues as a miss", exc)
            return None
        finally:
            if outcome is None:  # cancelled mid-call: says nothing about the API's health
                self.breaker.release(permit)
        EMBEDDING_SECONDS.observe(time.monotonic() - start)
        self.breaker.record_success(permit)
        return vector


def _pack(vector: List[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)  # Redis wants raw little-endian float32s


class RedisVectors:
    """Stored questions in redis-cache, searched with Redis's built-in vector search (O5)."""

    def __init__(self, client: redis.Redis, clock=time.time):
        self.redis = client
        self.clock = clock
        self.index_ready = False

    async def _ensure_index(self) -> None:
        # redis-cache has no persistence, so after a restart the index is gone too: create it on first use.
        if self.index_ready:
            return
        try:
            await self.redis.execute_command(
                "FT.CREATE", INDEX, "ON", "HASH", "PREFIX", "1", PREFIX, "SCHEMA",
                "partition", "TAG",
                # FLAT = exact search. The partition filter leaves a handful of candidates, so brute force is fine.
                "vector", "VECTOR", "FLAT", "6", "TYPE", "FLOAT32", "DIM", str(DIMENSIONS), "DISTANCE_METRIC", "COSINE",
            )
        except ResponseError as exc:
            if "already exists" not in str(exc):
                raise
        self.index_ready = True

    async def closest(self, partition: str, vector: List[float]) -> Optional[Match]:
        await self._ensure_index()
        try:
            reply = await self.redis.execute_command(
                "FT.SEARCH", INDEX, f"@partition:{{{partition}}}=>[KNN 1 @vector $vec AS distance]",
                "PARAMS", "2", "vec", _pack(vector),
                "RETURN", "5", "distance", "question", "body", "deployment", "stored_at", "DIALECT", "2",
            )
        except ResponseError as exc:
            if "no such index" not in str(exc).lower():
                raise
            # redis-cache restarted (or was flushed) under us. Not an outage: a miss, and storing this
            # request's answer recreates the index.
            self.index_ready = False
            return None
        if not reply or reply[0] == 0:
            return None
        key, raw = reply[1], reply[2]
        fields = {raw[i].decode(): raw[i + 1] for i in range(0, len(raw), 2)}
        hit = Hit(json.loads(fields["body"]), fields["deployment"].decode(),
                  max(0, int(self.clock() - float(fields["stored_at"]))))
        return Match(1 - float(fields["distance"]), fields["question"].decode(), key.decode(), hit)

    async def add(self, lookup: Lookup, vector: List[float], body: dict, deployment: str, ttl: int) -> None:
        await self._ensure_index()
        # Keyed by the question, so the same question in the same partition replaces its old answer.
        key = f"{PREFIX}{lookup.partition}:{hashlib.sha256(lookup.question.encode()).hexdigest()[:16]}"
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, mapping={
                "partition": lookup.partition, "vector": _pack(vector), "question": lookup.question,
                "body": json.dumps(body, separators=(",", ":")), "deployment": deployment, "stored_at": self.clock(),
            })
            pipe.expire(key, ttl)  # Redis drops expired hashes from the index on its own
            await pipe.execute()

    async def save_shadow(self, team: str, record: dict) -> None:
        key = f"shadow:{team}"  # outside the "sem:" prefix, so it isn't indexed
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.lpush(key, json.dumps(record, ensure_ascii=False))
            pipe.ltrim(key, 0, SHADOW_KEEP - 1)
            pipe.expire(key, SHADOW_TTL)
            await pipe.execute()

    async def shadow_records(self, team: str) -> List[dict]:
        return [json.loads(r) for r in await self.redis.lrange(f"shadow:{team}", 0, -1)]


class SemanticCache:
    def __init__(self, embedder: Embedder, vectors: RedisVectors):
        self.embedder = embedder
        self.vectors = vectors
        self.down_until = 0.0

    async def find(self, lookup: Lookup) -> Tuple[Optional[List[float]], Optional[Match]]:
        """(the question's vector, the closest stored question). No vector means the lookup was skipped."""
        if time.monotonic() < self.down_until:
            return None, None  # Redis is down: don't pay for an embedding we can't use
        vector = await self.embedder.embed(lookup.question)
        if vector is None:
            return None, None
        try:
            return vector, await self.vectors.closest(lookup.partition, vector)
        except (RedisError, OSError) as exc:
            self._failed(exc)
            return None, None

    async def put(self, lookup: Lookup, vector: List[float], body: dict, deployment: str, ttl: int) -> None:
        try:
            await self.vectors.add(lookup, vector, body, deployment, ttl)
        except (RedisError, OSError) as exc:
            self._failed(exc)

    async def save_shadow(self, team: str, record: dict) -> None:
        try:
            await self.vectors.save_shadow(team, record)
        except (RedisError, OSError) as exc:
            self._failed(exc)

    def _failed(self, exc) -> None:
        if time.monotonic() >= self.down_until:
            log.error("semantic cache: Redis failed (%r), every request is a miss for %.0f s", exc, REDIS_RETRY_AFTER)
        self.down_until = time.monotonic() + REDIS_RETRY_AFTER
