"""Rate limits: requests per minute and tokens per minute. Decisions: DECISIONS.md sections K and T2.

Each team has two token buckets in Redis, shared by every gateway copy. A key can have its own two as well,
inside its team's: every bucket a request touches is checked and taken together, in one script. A bucket holds up to one minute's
limit and refills continuously, so a team can burst up to its limit and then gets a steady rate.

Tokens are estimate-then-settle: before calling the provider we take a rough guess of the prompt's
tokens, and once the provider reports real usage we charge (or refund) the difference. A team that
used more than it had goes below zero and waits until the bucket refills.

If Redis is down we fail open, onto a bucket in this copy's memory with the limit split across copies.
"""
import json
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import redis.asyncio as redis
from redis.exceptions import RedisError

from metrics import RATE_LIMIT_DEGRADED

log = logging.getLogger("gateway")

CHARS_PER_TOKEN = 4  # rough rule for English text; settle() corrects it once real usage comes back
# An image's real cost depends on its size and detail setting (85 to ~1,500 tokens on OpenAI); this is a 1024x1024
# image at high detail. Counting the image's base64 text instead would make a 1 MB photo look like 250,000 tokens.
IMAGE_TOKENS = 765
REDIS_RETRY_AFTER = 5.0  # after a Redis error, use the local buckets for this long before trying Redis again


@dataclass(frozen=True)
class Limits:
    rpm: int
    tpm: int


@dataclass(frozen=True)
class Bucket:
    key: str  # in Redis
    cap: int  # per minute
    kind: str  # "requests" | "tokens", same words as OpenAI's 429
    scope: str  # "team" | "key"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining_requests: int  # in the tightest requests bucket: what a client can actually still send
    remaining_tokens: int
    limit_requests: int = 0  # that bucket's limit, for the x-ratelimit-limit-* headers
    limit_tokens: int = 0
    retry_after: float = 0.0  # seconds until the request would fit; only set when refused
    exceeded: Optional[Bucket] = None  # the bucket that refused it


def text_of(content) -> str:
    """The text in a message's content: a string, or OpenAI's list of parts ({"type": "text", "text": ...},
    {"type": "image_url", ...}). None for an assistant message that only calls tools."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p["text"] for p in content if isinstance(p, dict) and isinstance(p.get("text"), str))
    return ""


def estimate_tokens(messages: list) -> int:
    chars = images = 0
    for m in messages:
        content = m.get("content")
        chars += len(text_of(content))
        if isinstance(content, list):
            images += sum(1 for p in content if isinstance(p, dict) and p.get("type") == "image_url")
        if m.get("tool_calls"):
            chars += len(json.dumps(m["tool_calls"]))  # the arguments a model sent are input on the next turn
    # a few tokens of overhead per message
    return math.ceil(chars / CHARS_PER_TOKEN) + images * IMAGE_TOKENS + 4 * len(messages)


# All of a request's buckets are checked and taken in one script: Redis runs it by itself, so two gateway copies can
# never both spend the last coin, and a request refused by one bucket (say, its key's) takes nothing from the others
# (its team's). Each bucket is a hash {tokens, ts}. A missing bucket is a full one, which is also what an expired
# key becomes.
#   KEYS = the buckets
#   ARGV = now, force (1 = settle: charge even if it goes below zero), each bucket's cap, then each bucket's cost
#   returns {allowed, retry seconds, index of the bucket that refused (1-based, 0 = none), each bucket's tokens}
TAKE = """
local now = tonumber(ARGV[1])
local force = ARGV[2] == '1'
local n = #KEYS
local caps, costs, tokens = {}, {}, {}
local allowed, retry, exceeded = 1, 0, 0
for i = 1, n do
  local cap = tonumber(ARGV[2 + i])
  local cost = tonumber(ARGV[2 + n + i])
  local rate = cap / 60
  local b = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local t = tonumber(b[1]) or cap
  local ts = tonumber(b[2]) or now
  t = math.min(cap, t + math.max(0, now - ts) * rate)
  caps[i], costs[i], tokens[i] = cap, cost, t
  if not force and cost > 0 and t < cost then
    allowed = 0
    local wait = (cost - t) / rate
    if wait > retry then retry = wait; exceeded = i end
  end
end
local out = {allowed, 0, exceeded}
for i = 1, n do
  if allowed == 1 then tokens[i] = math.min(caps[i], tokens[i] - costs[i]) end
  redis.call('HSET', KEYS[i], 'tokens', tokens[i], 'ts', now)
  redis.call('EXPIRE', KEYS[i], 120)
  out[3 + i] = tostring(tokens[i])  -- Lua numbers become integers on the way out: send fractions as strings
end
out[2] = tostring(retry)
return out
"""


class LocalBuckets:
    """The same buckets in this process's memory. Only used while Redis is unreachable."""

    def __init__(self):
        self.buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, ts)

    def take(self, keys, now, caps, costs, force) -> list:
        """Same arguments and result as the script: [allowed, retry, exceeded, *tokens]."""
        tokens, allowed, retry, exceeded = [], 1, 0.0, 0
        for key, cap, cost in zip(keys, caps, costs):
            rate = cap / 60
            t, ts = self.buckets.get(key, (cap, now))
            t = min(cap, t + max(0.0, now - ts) * rate)
            tokens.append(t)
            if not force and cost > 0 and t < cost:
                allowed = 0
                wait = (cost - t) / rate
                if wait > retry:
                    retry, exceeded = wait, len(tokens)
        for i, (key, cap, cost) in enumerate(zip(keys, caps, costs)):
            if allowed:
                tokens[i] = min(cap, tokens[i] - cost)
            self.buckets[key] = (tokens[i], now)
        return [allowed, retry, exceeded, *tokens]


class RateLimiter:
    def __init__(self, client: redis.Redis, limits: Callable[[str], Limits], copies: int = 1, clock=time.time):
        self.redis = client
        self.limits = limits  # team -> Limits
        self.copies = copies  # how many gateway copies share the limit, for the local fallback
        self.clock = clock  # wall clock, not monotonic: every gateway copy writes timestamps into the same bucket
        self.script = client.register_script(TAKE)
        self.local = LocalBuckets()
        self.redis_down_until = 0.0

    def buckets(self, team: str, key=None) -> List[Bucket]:
        """The team's two buckets, plus the key's own if it has limits (a keys.KeyInfo, or None)."""
        limits = self.limits(team)
        buckets = [Bucket(f"rl:{team}:rpm", limits.rpm, "requests", "team"),
                   Bucket(f"rl:{team}:tpm", limits.tpm, "tokens", "team")]
        if key is not None and key.rpm:
            buckets.append(Bucket(f"rl:key:{key.id}:rpm", key.rpm, "requests", "key"))
        if key is not None and key.tpm:
            buckets.append(Bucket(f"rl:key:{key.id}:tpm", key.tpm, "tokens", "key"))
        return buckets

    async def check(self, team: str, estimate: int, key=None) -> Decision:
        """Take 1 request and the estimated tokens from every bucket, or refuse without taking anything."""
        buckets = self.buckets(team, key)
        allowed, retry, exceeded, left = await self._take(buckets, 1, estimate, force=False)
        tightest = {}
        for bucket, tokens in zip(buckets, left):
            if bucket.kind not in tightest or tokens < tightest[bucket.kind][1]:
                tightest[bucket.kind] = (bucket, tokens)
        return Decision(
            allowed=bool(allowed),
            remaining_requests=max(0, int(tightest["requests"][1])),
            remaining_tokens=max(0, int(tightest["tokens"][1])),
            limit_requests=tightest["requests"][0].cap,
            limit_tokens=tightest["tokens"][0].cap,
            retry_after=retry,
            exceeded=buckets[exceeded - 1] if exceeded else None,
        )

    async def settle(self, team: str, estimate: int, usage: Optional[dict], key=None) -> None:
        """Charge the difference between real usage and the estimate to every tokens bucket. Negative is a refund."""
        if not usage:  # stream cut short, or an error the client must fix: keep the estimate as the charge
            return
        diff = usage.get("total_tokens", 0) - estimate
        if diff:
            await self._take(self.buckets(team, key), 0, diff, force=True)

    async def _take(self, buckets: List[Bucket], requests: int, tokens: int, force: bool) -> tuple:
        keys = [b.key for b in buckets]
        costs = [requests if b.kind == "requests" else tokens for b in buckets]
        now = self.clock()
        if now >= self.redis_down_until:
            try:
                result = await self.script(keys=keys, args=[now, int(force), *(b.cap for b in buckets), *costs])
                if self.redis_down_until:
                    log.warning("rate limiter: Redis is back, using shared limits again")
                    self.redis_down_until = 0.0
                    RATE_LIMIT_DEGRADED.set(0)
                return int(result[0]), float(result[1]), int(result[2]), [float(t) for t in result[3:]]
            except (RedisError, OSError) as exc:
                if not self.redis_down_until:
                    log.error("rate limiter: Redis unreachable (%r), failing open to local limits", exc)
                self.redis_down_until = now + REDIS_RETRY_AFTER
                RATE_LIMIT_DEGRADED.set(1)
        # Fail open, but not unlimited: each copy enforces its share of every limit.
        caps = [max(1, b.cap // self.copies) for b in buckets]
        allowed, retry, exceeded, *left = self.local.take(keys, now, caps, costs, force)
        return allowed, retry, exceeded, left

    async def close(self):
        await self.redis.aclose()
