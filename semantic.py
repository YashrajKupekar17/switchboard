"""Semantic cache: a question that means the same as a stored one gets the stored answer. Decisions: DECISIONS.md section O.

Runs after the exact cache misses, only for teams that turned it on (or to shadow mode, which looks but never serves):

    request -> partition (exact hash of everything except the last user message) + question (that message)
            -> embed the question (OpenAI, 1 s timeout, circuit breaker)
            -> closest stored question in the same partition (Redis vector search)
            -> similarity >= the team's threshold ? ask the judge : miss
            -> the judge (gpt-4o-mini) reads both questions: the same question ? hit : miss

The embedding only finds a candidate. It can't decide: on our test pairs, different questions scored higher than
real paraphrases (O7). The judge reads both questions together and decides (O9, O10).

Every failure on the way is a miss. The cache is there to save money; it must never be why a request fails or slows down.
"""
import hashlib
import json
import logging
import math
import struct
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import httpx
import redis.asyncio as redis
from redis.exceptions import RedisError, ResponseError

from breaker import CircuitBreaker
from cache import NOT_IN_KEY, Hit
from metrics import SEMANTIC_STEP_SECONDS

log = logging.getLogger("gateway")

MODES = ("off", "shadow", "on")
EMBED_MODEL = "text-embedding-3-small"
DIMENSIONS = 1536
EMBED_TIMEOUT = 1.0  # just above the call's normal slowest, measured (O4): slower counts as a miss
JUDGE_MODEL = "gpt-4o-mini"
JUDGE_TIMEOUT = 1.5  # measured ~660 ms typical, 3.5 s worst (O9)
JUDGE_MIN_YES = 0.9  # probability of "yes" needed to serve: a miss costs money, a wrong hit costs trust
# Neutral on purpose: a prompt that lists the traps scores well on the test set and nowhere else (O9).
JUDGE_PROMPT = ("Two users sent these questions to an assistant.\n"
                "Question A: {a}\nQuestion B: {b}\n\n"
                "Are these the same question, so that the same answer would be correct for both? "
                "Reply with only yes or no.")
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


class OpenAIStep:
    """One OpenAI call on the way to a cache decision: a short timeout, its own breaker, None on any failure (O4)."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str, path: str, step: str, model: str,
                 timeout: float):
        self.http = http
        self.url = f"{base_url.rstrip('/')}/{path}"
        self.api_key = api_key
        self.step = step
        self.timeout = timeout
        # The same breaker as for providers: once the API is failing, stop paying the timeout on every request.
        self.breaker = CircuitBreaker(f"{step}/{model}", failure_threshold=3)

    async def _post(self, body: dict, parse: Callable[[dict], object]):
        permit = self.breaker.allow()
        if permit is None:
            return None
        outcome = None
        start = time.monotonic()
        try:
            response = await self.http.post(self.url, json=body, headers={"Authorization": f"Bearer {self.api_key}"},
                                            timeout=self.timeout)
            response.raise_for_status()
            result = parse(response.json())
            outcome = "ok"
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            outcome = "failed"
            self.breaker.record_failure(permit)
            log.warning("semantic %s failed (%r): request continues as a miss", self.step, exc)
            return None
        finally:
            if outcome is None:  # cancelled mid-call: says nothing about the API's health
                self.breaker.release(permit)
        SEMANTIC_STEP_SECONDS.labels(self.step).observe(time.monotonic() - start)
        self.breaker.record_success(permit)
        return result


class Embedder(OpenAIStep):
    """Question -> vector, via OpenAI's embeddings API."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str):
        super().__init__(http, base_url, api_key, "embeddings", "embed", EMBED_MODEL, EMBED_TIMEOUT)

    async def embed(self, text: str) -> Optional[List[float]]:
        return await self._post({"model": EMBED_MODEL, "input": text}, lambda d: d["data"][0]["embedding"])


class Judge(OpenAIStep):
    """Two questions -> the probability that they're the same question, from an LLM reading both (O10)."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, api_key: str):
        super().__init__(http, base_url, api_key, "chat/completions", "judge", JUDGE_MODEL, JUDGE_TIMEOUT)

    async def same(self, a: str, b: str) -> Optional[float]:
        # One output token, and logprobs to read how sure it is instead of a bare yes or no.
        return await self._post({
            "model": JUDGE_MODEL, "temperature": 0, "max_tokens": 1, "logprobs": True, "top_logprobs": 5,
            "messages": [{"role": "user", "content": JUDGE_PROMPT.format(a=a, b=b)}],
        }, _probability_of_yes)


def _probability_of_yes(response: dict) -> float:
    top = response["choices"][0]["logprobs"]["content"][0]["top_logprobs"]
    return sum(math.exp(t["logprob"]) for t in top if t["token"].strip().lower() == "yes")


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
    def __init__(self, embedder: Embedder, judge: Judge, vectors: RedisVectors):
        self.embedder = embedder
        self.judge = judge
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
