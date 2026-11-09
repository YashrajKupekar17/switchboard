"""Per-team rate limits: requests per minute and tokens per minute. Decisions: DECISIONS.md section K.

Each team has two token buckets in Redis, shared by every gateway copy. A bucket holds up to one minute's
limit and refills continuously, so a team can burst up to its limit and then gets a steady rate.

Tokens are estimate-then-settle: before calling the provider we take a rough guess of the prompt's
tokens, and once the provider reports real usage we charge (or refund) the difference. A team that
used more than it had goes below zero and waits until the bucket refills.

If Redis is down we fail open, onto a bucket in this copy's memory with the limit split across copies.
"""
import logging
import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

import redis.asyncio as redis
from redis.exceptions import RedisError

from metrics import RATE_LIMIT_DEGRADED

log = logging.getLogger("gateway")

CHARS_PER_TOKEN = 4  # rough rule for English text; settle() corrects it once real usage comes back
REDIS_RETRY_AFTER = 5.0  # after a Redis error, use the local buckets for this long before trying Redis again


@dataclass(frozen=True)
class Limits:
    rpm: int
    tpm: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining_requests: int
    remaining_tokens: int
    retry_after: float = 0.0  # seconds until the request would fit; only set when refused
    exceeded: str = ""  # "requests" or "tokens", same words as OpenAI's 429


def estimate_tokens(messages: list) -> int:
    chars = sum(len(str(m.get("content") or "")) for m in messages)
    return math.ceil(chars / CHARS_PER_TOKEN) + 4 * len(messages)  # a few tokens of overhead per message


# Both buckets are checked and taken in one script: Redis runs it by itself, so two gateway copies can never
# both spend the last coin, and a request refused by one bucket doesn't take coins from the other.
# Each bucket is a hash {tokens, ts}. A missing bucket is a full one, which is also what an expired key becomes.
#   KEYS = rpm bucket, tpm bucket
#   ARGV = now, rpm, tpm, request cost, token cost, force (1 = settle: charge even if it goes below zero)
TAKE = """
local now = tonumber(ARGV[1])
local force = ARGV[6] == '1'
local caps = {tonumber(ARGV[2]), tonumber(ARGV[3])}
local costs = {tonumber(ARGV[4]), tonumber(ARGV[5])}
local tokens = {}
local allowed = 1
local retry = 0
local exceeded = 0
for i = 1, 2 do
  local cap = caps[i]
  local rate = cap / 60
  local b = redis.call('HMGET', KEYS[i], 'tokens', 'ts')
  local t = tonumber(b[1]) or cap
  local ts = tonumber(b[2]) or now
  t = math.min(cap, t + math.max(0, now - ts) * rate)
  tokens[i] = t
  if not force and costs[i] > 0 and t < costs[i] then
    allowed = 0
    local wait = (costs[i] - t) / rate
    if wait > retry then retry = wait; exceeded = i end
  end
end
for i = 1, 2 do
  if allowed == 1 then tokens[i] = math.min(caps[i], tokens[i] - costs[i]) end
  redis.call('HSET', KEYS[i], 'tokens', tokens[i], 'ts', now)
  redis.call('EXPIRE', KEYS[i], 120)
end
-- Lua numbers become integers on the way out, so send the fractional values as strings
return {allowed, tostring(tokens[1]), tostring(tokens[2]), tostring(retry), exceeded}
"""


class LocalBuckets:
    """The same buckets in this process's memory. Only used while Redis is unreachable."""

    def __init__(self):
        self.buckets: Dict[str, Tuple[float, float]] = {}  # key -> (tokens, ts)

    def take(self, keys, now, caps, costs, force) -> list:
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
        return [allowed, *tokens, retry, exceeded]


class RateLimiter:
    def __init__(self, client: redis.Redis, limits: Callable[[str], Limits], copies: int = 1, clock=time.time):
        self.redis = client
        self.limits = limits  # team -> Limits
        self.copies = copies  # how many gateway copies share the limit, for the local fallback
        self.clock = clock  # wall clock, not monotonic: every gateway copy writes timestamps into the same bucket
        self.script = client.register_script(TAKE)
        self.local = LocalBuckets()
        self.redis_down_until = 0.0

    async def check(self, team: str, estimate: int) -> Decision:
        """Take 1 request and the estimated tokens, or refuse without taking anything."""
        limits = self.limits(team)
        allowed, rpm_left, tpm_left, retry, exceeded = await self._take(team, limits, 1, estimate, force=False)
        return Decision(
            allowed=bool(allowed),
            remaining_requests=max(0, int(rpm_left)),
            remaining_tokens=max(0, int(tpm_left)),
            retry_after=retry,
            exceeded={1: "requests", 2: "tokens"}.get(exceeded, ""),
        )

    async def settle(self, team: str, estimate: int, usage: Optional[dict]) -> None:
        """Charge the difference between real usage and the estimate. Negative means a refund."""
        if not usage:  # stream cut short, or an error the client must fix: keep the estimate as the charge
            return
        diff = usage.get("total_tokens", 0) - estimate
        if diff:
            await self._take(team, self.limits(team), 0, diff, force=True)

    async def _take(self, team, limits, requests, tokens, force) -> list:
        keys = [f"rl:{team}:rpm", f"rl:{team}:tpm"]
        now = self.clock()
        if now >= self.redis_down_until:
            try:
                args = [now, limits.rpm, limits.tpm, requests, tokens, int(force)]
                result = await self.script(keys=keys, args=args)
                if self.redis_down_until:
                    log.warning("rate limiter: Redis is back, using shared limits again")
                    self.redis_down_until = 0.0
                    RATE_LIMIT_DEGRADED.set(0)
                return [int(result[0]), float(result[1]), float(result[2]), float(result[3]), int(result[4])]
            except (RedisError, OSError) as exc:
                if not self.redis_down_until:
                    log.error("rate limiter: Redis unreachable (%r), failing open to local limits", exc)
                self.redis_down_until = now + REDIS_RETRY_AFTER
                RATE_LIMIT_DEGRADED.set(1)
        # Fail open, but not unlimited: each copy enforces its share of the team's limit.
        caps = [max(1, limits.rpm // self.copies), max(1, limits.tpm // self.copies)]
        return self.local.take(keys, now, caps, [requests, tokens], force)

    async def close(self):
        await self.redis.aclose()
