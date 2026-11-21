"""Monthly spend budgets per team. Decisions: DECISIONS.md section L.

Postgres is the source of truth: an append-only ledger with one row per request. Each gateway copy
buffers rows in memory and writes them in one batch every second.

Redis holds each team's running total for the month, so the check on every request is one GET instead
of a SUM over the ledger. The total is only a cache: it expires every 5 minutes and is rebuilt from the
ledger, so anything it missed (a Redis outage, a restart) is corrected within 5 minutes.

A team over `soft_usd` gets a warning; at `max_usd` its requests are refused until the month resets.
A key can also have its own monthly `max_usd`, inside its team's (T2), so one runaway service can't spend the
whole team's budget. It's kept the same way: a running total per key, rebuilt from the ledger by key_id.
"""
import asyncio
import datetime as dt
import json
import logging
import time
from dataclasses import dataclass
from typing import List, Optional

import asyncpg
import redis.asyncio as redis
from redis.exceptions import RedisError

from pricing import usd
from metrics import BUDGET_SPENT, BUDGET_USED, LEDGER_BUFFERED, LEDGER_DROPPED

log = logging.getLogger("gateway")

TOTAL_TTL = 300  # seconds a Redis total lives before it is rebuilt from the ledger
FLUSH_EVERY = 1.0  # seconds between ledger writes
MAX_BUFFER = 100_000  # rows kept while Postgres is down (~5 minutes at 300 RPS); after that, the oldest are dropped
REDIS_RETRY_AFTER = 5.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_ledger (
    id                  BIGSERIAL PRIMARY KEY,
    created_at          TIMESTAMPTZ NOT NULL,
    team                TEXT NOT NULL,
    key_id              BIGINT NOT NULL,
    model               TEXT NOT NULL,        -- what the client asked for
    deployment          TEXT NOT NULL,        -- what actually answered, which is what we pay for
    uncached_tokens     INTEGER NOT NULL,
    cached_tokens       INTEGER NOT NULL,
    cache_write_tokens  INTEGER NOT NULL,
    output_tokens       INTEGER NOT NULL,
    cost_micros         BIGINT NOT NULL,      -- priced when the request ran; later price changes never rewrite it
    estimated           BOOLEAN NOT NULL      -- true when the provider never reported usage (stream cut short)
);
CREATE INDEX IF NOT EXISTS usage_ledger_team_time ON usage_ledger (team, created_at);
-- Added with the response cache (DECISIONS.md N). A hit costs nothing; saved_micros is what it would have cost.
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS cache_hit BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS saved_micros BIGINT NOT NULL DEFAULT 0;
-- Per-key totals (DECISIONS.md T2) are rebuilt with a SUM over one key's month, like a team's.
CREATE INDEX IF NOT EXISTS usage_ledger_key_time ON usage_ledger (key_id, created_at);
-- Request tags (DECISIONS.md T1), e.g. {"feature": "search", "env": "prod"}. NULL when the request had none.
ALTER TABLE usage_ledger ADD COLUMN IF NOT EXISTS tags JSONB;
"""

COLUMNS = ["created_at", "team", "key_id", "model", "deployment", "uncached_tokens", "cached_tokens",
           "cache_write_tokens", "output_tokens", "cost_micros", "estimated", "cache_hit", "saved_micros", "tags"]

# Add to the month's total only if it exists. If it doesn't, the next check rebuilds it from the ledger,
# which will include this row; adding here would create a total that's missing the rest of the month.
ADD_IF_EXISTS = """
for _, key in ipairs(KEYS) do
  if redis.call('EXISTS', key) == 1 then redis.call('INCRBY', key, ARGV[1]) end
end
return 0
"""


@dataclass(frozen=True)
class Budget:
    soft_usd: float
    max_usd: float

    @property
    def soft_micros(self) -> int:
        return round(self.soft_usd * 1_000_000)

    @property
    def max_micros(self) -> int:
        return round(self.max_usd * 1_000_000)


@dataclass(frozen=True)
class Spend:
    spent_micros: Optional[int]  # the team's this month; None when Redis is down and we couldn't check
    allowed: bool
    key_spent_micros: Optional[int] = None  # only for a key with its own budget
    key_max_micros: Optional[int] = None
    refused_by: str = ""  # "team" | "key", when not allowed


def month_of(now: dt.datetime) -> str:
    return now.strftime("%Y-%m")


def month_start(now: dt.datetime) -> dt.datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)  # budget months are UTC, so every gateway copy agrees


class PostgresLedger:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def insert(self, rows: List[tuple]) -> None:
        await self.pool.copy_records_to_table("usage_ledger", records=rows, columns=COLUMNS)  # one round trip

    async def key_month_total(self, key_id: int, since: dt.datetime) -> int:
        total = await self.pool.fetchval(
            "SELECT COALESCE(SUM(cost_micros), 0) FROM usage_ledger WHERE key_id = $1 AND created_at >= $2",
            key_id, since,
        )
        return int(total)

    async def month_total(self, team: str, since: dt.datetime) -> int:
        # SUM of a BIGINT is a NUMERIC in Postgres (it could overflow BIGINT), which asyncpg returns as Decimal.
        total = await self.pool.fetchval(
            "SELECT COALESCE(SUM(cost_micros), 0) FROM usage_ledger WHERE team = $1 AND created_at >= $2",
            team, since,
        )
        return int(total)


async def open_ledger(pool: asyncpg.Pool) -> PostgresLedger:
    if await pool.fetchval("SELECT to_regclass('usage_ledger')") is None:  # made by scripts/setup_db.py
        raise RuntimeError("table usage_ledger doesn't exist: run scripts/setup_db.py first")
    return PostgresLedger(pool)


class Budgets:
    def __init__(self, client: redis.Redis, ledger, budget_for, clock=utcnow):
        self.redis = client
        self.ledger = ledger
        self.budget_for = budget_for  # team -> Budget
        self.clock = clock
        self.add_if_exists = client.register_script(ADD_IF_EXISTS)
        self.buffer: List[tuple] = []
        self.redis_down_until = 0.0
        self._flusher: Optional[asyncio.Task] = None

    # --- the check, before each request ---

    async def check(self, team: str, key=None) -> Spend:
        """The team's budget, then the key's own if it has one (a keys.KeyInfo, or None)."""
        budget = self.budget_for(team)
        now = self.clock()
        month = month_of(now)
        spent = await self._total(f"spend:{team}:{month}", lambda: self.ledger.month_total(team, month_start(now)),
                                  f"team {team}")
        if spent is None:
            return Spend(None, True)  # fail open: the ledger still records everything (L5)
        BUDGET_SPENT.labels(team).set(spent / 1_000_000)
        BUDGET_USED.labels(team).set(spent / budget.soft_micros if budget.soft_micros else 0)
        if spent >= budget.soft_micros:
            await self._warn_once(team, now, spent, budget)
        if spent >= budget.max_micros:
            return Spend(spent, False, refused_by="team")
        if key is None or key.max_usd is None:
            return Spend(spent, True)

        key_max = round(key.max_usd * 1_000_000)
        key_spent = await self._total(f"spend:key:{key.id}:{month}",
                                      lambda: self.ledger.key_month_total(key.id, month_start(now)), f"key {key.id}")
        if key_spent is None:
            return Spend(spent, True)
        if key_spent >= key_max:
            return Spend(spent, False, key_spent, key_max, refused_by="key")
        return Spend(spent, True, key_spent, key_max)

    async def _total(self, redis_key: str, rebuild, what: str) -> Optional[int]:
        """A running total from Redis, rebuilt from the ledger if it's missing. None if we couldn't tell."""
        if time.monotonic() < self.redis_down_until:
            return None
        try:
            total = await self.redis.get(redis_key)
            if total is None:
                total = await rebuild()
                # NX: if another copy rebuilt it first, keep theirs, and read back whichever won.
                await self.redis.set(redis_key, total, nx=True, ex=TOTAL_TTL)
                total = await self.redis.get(redis_key) or total
            return int(total)
        except (RedisError, OSError) as exc:
            self._redis_failed(exc)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, asyncio.TimeoutError) as exc:
            log.warning("budget: couldn't rebuild the total for %s from the ledger: %r", what, exc)
        return None

    async def _warn_once(self, team, now, spent, budget):
        try:
            # SET NX succeeds only the first time this month, so the warning isn't repeated on every request.
            if await self.redis.set(f"budget-warned:{team}:{month_of(now)}", 1, nx=True, ex=32 * 86400):
                log.warning("budget: team %s has spent %s of its %s soft budget (hard stop at %s)",
                            team, usd(spent / 1_000_000), usd(budget.soft_usd), usd(budget.max_usd))
        except (RedisError, OSError) as exc:
            self._redis_failed(exc)

    # --- recording, after each request ---

    async def record(self, *, team, key_id, model, deployment, tokens, cost_micros, estimated,
                     cache_hit=False, saved_micros=0, tags=None) -> None:
        now = self.clock()
        self.buffer.append((now, team, key_id, model, deployment, tokens.uncached, tokens.cached,
                            tokens.cache_write, tokens.output, cost_micros, estimated, cache_hit, saved_micros,
                            json.dumps(tags) if tags else None))  # asyncpg takes JSONB as text
        if len(self.buffer) > MAX_BUFFER:
            dropped = len(self.buffer) - MAX_BUFFER
            del self.buffer[:dropped]
            LEDGER_DROPPED.inc(dropped)
            log.error("budget: ledger buffer full, dropped %d rows", dropped)
        LEDGER_BUFFERED.set(len(self.buffer))
        if cost_micros and time.monotonic() >= self.redis_down_until:
            try:
                month = month_of(now)  # the team's total and the key's (if it's tracked), in one call
                await self.add_if_exists(keys=[f"spend:{team}:{month}", f"spend:key:{key_id}:{month}"],
                                         args=[cost_micros])
            except (RedisError, OSError) as exc:
                self._redis_failed(exc)

    async def flush(self) -> None:
        if not self.buffer:
            return
        rows, self.buffer = self.buffer, []
        try:
            await self.ledger.insert(rows)
        except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError, asyncio.TimeoutError) as exc:
            log.error("budget: ledger write failed, keeping %d rows to retry: %r", len(rows), exc)
            self.buffer = rows + self.buffer
        LEDGER_BUFFERED.set(len(self.buffer))

    def start(self) -> None:
        async def loop():
            while True:
                await asyncio.sleep(FLUSH_EVERY)
                await self.flush()
        self._flusher = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._flusher:
            self._flusher.cancel()
        await self.flush()  # don't lose the last second on a clean shutdown

    def _redis_failed(self, exc) -> None:
        if time.monotonic() >= self.redis_down_until:
            log.error("budget: Redis unreachable (%r), skipping budget checks; spend is still recorded", exc)
        self.redis_down_until = time.monotonic() + REDIS_RETRY_AFTER
