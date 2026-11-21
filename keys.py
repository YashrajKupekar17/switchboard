"""Team API keys. Decisions: DECISIONS.md sections J and T.

A key looks like sbk_ + 32 random characters. It is shown once, when created. Postgres stores only its
SHA-256 hash, plus a short visible prefix so people can tell keys apart. Each gateway copy keeps recent
answers in memory: valid for 60 s, invalid for 10 s. Only a cache miss goes to Postgres.

A key can also carry its own limits and monthly budget, inside its team's (T2), and an expiry date (T3).
Every change an admin makes is written to key_audit in the same transaction as the change itself.
"""
import asyncio
import datetime as dt
import hashlib
import json
import logging
import secrets
import string
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Tuple

import asyncpg

log = logging.getLogger("gateway")

PREFIX = "sbk_"
RANDOM_CHARS = 32
KEY_LENGTH = len(PREFIX) + RANDOM_CHARS
VISIBLE_CHARS = len(PREFIX) + 8  # "sbk_8f3kQ9x2": enough to recognise a key, far too little to guess the rest
ALPHABET = string.ascii_letters + string.digits  # 62^32 ≈ 2^190 possible keys

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id          BIGSERIAL PRIMARY KEY,
    key_hash    TEXT NOT NULL UNIQUE,           -- UNIQUE also gives us the index the lookup needs
    prefix      TEXT NOT NULL,
    team        TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ                     -- soft delete: spend history still points at this row
);
-- Per-key settings (DECISIONS.md T2, T3). NULL means "only the team's": a key can be tighter than its team, never
-- looser, because both are checked.
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS rpm          INTEGER CHECK (rpm > 0);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS tpm          INTEGER CHECK (tpm > 0);
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS max_usd      NUMERIC(14, 6) CHECK (max_usd >= 0);   -- per month, UTC
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS expires_at   TIMESTAMPTZ;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;   -- to the minute: written in batches
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS rotated_from BIGINT REFERENCES api_keys (id);

-- Who changed which key, when, and how. Written only by the admin tool, in the change's own transaction.
CREATE TABLE IF NOT EXISTS key_audit (
    id       BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor    TEXT NOT NULL,
    action   TEXT NOT NULL,                  -- create | revoke | rotate | update
    key_id   BIGINT NOT NULL REFERENCES api_keys (id),
    details  JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS key_audit_key ON key_audit (key_id, at);
-- An earlier version made max_usd NUMERIC(12, 2), which rounded sub-cent budgets to 0 while the audit log kept
-- the value asked for. Money is counted in micro-dollars everywhere else, so the column is too. A no-op if it is.
ALTER TABLE api_keys ALTER COLUMN max_usd TYPE NUMERIC(14, 6);
"""

KEY_COLUMNS = "id, team, prefix, name, rpm, tpm, max_usd, expires_at"
SETTINGS = ("rpm", "tpm", "max_usd", "expires_at")  # what an admin can set on a key


@dataclass(frozen=True)
class KeyInfo:
    id: int
    team: str
    prefix: str
    name: str = ""
    rpm: Optional[int] = None  # None: only the team's limits apply
    tpm: Optional[int] = None
    max_usd: Optional[Decimal] = None  # None: only the team's budget applies
    expires_at: Optional[dt.datetime] = None

    @property
    def label(self) -> str:
        """How people recognise the key in messages and reports: its prefix, and its name if it has one."""
        return f"{self.prefix} ({self.name})" if self.name else self.prefix

    def expired(self, now: dt.datetime) -> bool:
        return self.expires_at is not None and self.expires_at <= now


def _info(row) -> KeyInfo:
    return KeyInfo(row["id"], row["team"], row["prefix"], row["name"], row["rpm"], row["tpm"], row["max_usd"],
                   row["expires_at"])


class AuthUnavailable(Exception):
    """Postgres couldn't answer. We reject the request (fail closed) rather than let unknown keys in."""


def generate_key() -> str:
    return PREFIX + "".join(secrets.choice(ALPHABET) for _ in range(RANDOM_CHARS))


def hash_key(key: str) -> str:
    # A fast hash is enough: random keys can't be guessed, so a slow hash like bcrypt would only add latency.
    return hashlib.sha256(key.encode()).hexdigest()


def looks_like_a_key(key: str) -> bool:
    return len(key) == KEY_LENGTH and key.startswith(PREFIX)


class PostgresKeys:
    """The source of truth. find() and touch() are the gateway's; the rest is the admin tool's (scripts/keys.py),
    which runs as the database owner. The gateway's own user can't create, change or revoke keys (M3)."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def find(self, key_hash: str) -> Optional[KeyInfo]:
        # Expired keys are found too, so the gateway can say "expired" rather than "invalid" (T3).
        row = await self.pool.fetchrow(
            f"SELECT {KEY_COLUMNS} FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL", key_hash
        )
        return _info(row) if row else None

    async def touch(self, seen: Dict[int, dt.datetime]) -> None:
        """Record when keys were last used: one statement for the whole batch (T3)."""
        await self.pool.execute(
            "UPDATE api_keys k SET last_used_at = GREATEST(k.last_used_at, u.at) "
            "FROM unnest($1::bigint[], $2::timestamptz[]) AS u(id, at) WHERE k.id = u.id",
            list(seen), list(seen.values()),
        )

    # --- admin: each change and its audit row commit together, or neither does ---

    async def create(self, team: str, name: str = "", *, actor: str = "unknown", rotated_from: Optional[int] = None,
                     **settings) -> Tuple[KeyInfo, str]:
        async with self.pool.acquire() as conn, conn.transaction():
            return await self._create(conn, team, name, actor, rotated_from, settings)

    async def _create(self, conn, team, name, actor, rotated_from, settings) -> Tuple[KeyInfo, str]:
        key = generate_key()
        values = {s: settings.get(s) for s in SETTINGS}
        row = await conn.fetchrow(
            f"INSERT INTO api_keys (key_hash, prefix, team, name, rotated_from, {', '.join(SETTINGS)}) "
            f"VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) RETURNING {KEY_COLUMNS}",
            hash_key(key), key[:VISIBLE_CHARS], team, name, rotated_from, *values.values(),
        )
        await self._audit(conn, actor, "create", row["id"],
                          {"team": team, "name": name, "rotated_from": rotated_from, **values})
        return _info(row), key  # the only time the full key exists

    async def revoke(self, key_id: int, *, actor: str = "unknown", reason: str = "") -> bool:
        async with self.pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL", key_id
            )
            if result != "UPDATE 1":
                return False
            await self._audit(conn, actor, "revoke", key_id, {"reason": reason})
            return True

    async def rotate(self, key_id: int, grace: dt.timedelta, *, actor: str = "unknown") -> Tuple[KeyInfo, str]:
        """A new key with the same team, name and settings. The old one keeps working for `grace`, so services
        can move to the new key without an outage, then expires. Raises LookupError if there's no such active key."""
        async with self.pool.acquire() as conn, conn.transaction():
            old = await conn.fetchrow(
                f"SELECT {KEY_COLUMNS}, created_at, now() AS now FROM api_keys "
                "WHERE id = $1 AND revoked_at IS NULL FOR UPDATE", key_id
            )
            if old is None:
                raise LookupError(f"no active key with id {key_id}")
            settings = {s: old[s] for s in SETTINGS}
            if old["expires_at"]:  # the same lifetime as the old key, from now: rotating is how a key gets renewed
                settings["expires_at"] = old["now"] + (old["expires_at"] - old["created_at"])
            info, key = await self._create(conn, old["team"], old["name"], actor, key_id, settings)
            # Never extend an old key's life: if it already expires sooner, keep that.
            ends = await conn.fetchval(
                "UPDATE api_keys SET expires_at = LEAST(coalesce(expires_at, 'infinity'), now() + $2::interval) "
                "WHERE id = $1 RETURNING expires_at", key_id, grace,
            )
            await self._audit(conn, actor, "rotate", key_id, {"replaced_by": info.id, "expires_at": ends})
            return info, key

    async def update(self, key_id: int, *, actor: str = "unknown", **changes) -> bool:
        """Set rpm, tpm, max_usd or expires_at; None clears one. Takes effect within a minute (the key cache)."""
        unknown = set(changes) - set(SETTINGS)
        if unknown or not changes:
            raise ValueError(f"can only change {', '.join(SETTINGS)}, got {', '.join(sorted(unknown)) or 'nothing'}")
        names = list(changes)  # from SETTINGS only, never from input text: safe to put in the SQL
        assignments = ", ".join(f"{n} = ${i + 2}" for i, n in enumerate(names))
        async with self.pool.acquire() as conn, conn.transaction():
            result = await conn.execute(
                f"UPDATE api_keys SET {assignments} WHERE id = $1 AND revoked_at IS NULL", key_id, *changes.values()
            )
            if result != "UPDATE 1":
                return False
            await self._audit(conn, actor, "update", key_id, changes)
            return True

    async def list(self, team: Optional[str] = None) -> List[asyncpg.Record]:
        return await self.pool.fetch(
            f"SELECT {KEY_COLUMNS}, created_at, revoked_at, last_used_at FROM api_keys "
            "WHERE $1::text IS NULL OR team = $1 ORDER BY id", team,
        )

    async def audit(self, key_id: Optional[int] = None, limit: int = 50) -> List[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT at, actor, action, key_id, details FROM key_audit WHERE $1::bigint IS NULL OR key_id = $1 "
            "ORDER BY at DESC, id DESC LIMIT $2", key_id, limit,
        )

    @staticmethod
    async def _audit(conn, actor: str, action: str, key_id: int, details: dict) -> None:
        await conn.execute(
            "INSERT INTO key_audit (actor, action, key_id, details) VALUES ($1, $2, $3, $4)",
            actor, action, key_id, json.dumps(details, default=str),  # default=str: Decimal and datetime
        )

    async def close(self) -> None:
        await self.pool.close()


async def connect(dsn: str) -> PostgresKeys:
    # A small pool, opened once: a new connection per query would cost 5-20 ms; a reused one about 1 ms.
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=5, command_timeout=2)
    # The gateway's database user can't create tables (least privilege, DECISIONS.md M3), so tables are made
    # by scripts/setup_db.py. Check they exist now, rather than failing on the first request.
    if await pool.fetchval("SELECT to_regclass('api_keys')") is None:
        await pool.close()
        raise RuntimeError("table api_keys doesn't exist: run scripts/setup_db.py first")
    return PostgresKeys(pool)


class KeyCache:
    """Cache-aside in front of the key source, in this process's memory.

    Each gateway copy fills its own cache, so it doesn't matter which copy a request reaches.
    No locks: one event loop, and the dict is never touched across an await.
    """

    MAX_INVALID = 10_000  # random made-up keys must not be able to fill the gateway's memory

    def __init__(self, source, valid_ttl: float = 60.0, invalid_ttl: float = 10.0,
                 clock: Callable[[], float] = time.monotonic):
        self.source = source
        self.valid_ttl, self.invalid_ttl = valid_ttl, invalid_ttl
        self.clock = clock
        self._valid: dict = {}    # key_hash -> (KeyInfo, expires_at)
        self._invalid: dict = {}  # key_hash -> expires_at

    async def check(self, key: str) -> Optional[KeyInfo]:
        """The key's owner, or None if it isn't an active key. Raises AuthUnavailable if Postgres is down."""
        if not looks_like_a_key(key):
            return None  # not even shaped like one of our keys: no need to ask the database
        key_hash = hash_key(key)
        now = self.clock()

        hit = self._valid.get(key_hash)
        if hit and hit[1] > now:
            return hit[0]
        if self._invalid.get(key_hash, 0) > now:
            return None

        try:
            info = await self.source.find(key_hash)
        except (OSError, asyncio.TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            raise AuthUnavailable(repr(exc)) from exc

        now = self.clock()
        if info:
            self._valid[key_hash] = (info, now + self.valid_ttl)
            self._invalid.pop(key_hash, None)
        else:
            if len(self._invalid) >= self.MAX_INVALID:
                self._invalid.clear()  # crude but bounded; worst case a few more database queries
            self._invalid[key_hash] = now + self.invalid_ttl
            self._valid.pop(key_hash, None)
        return info


class LastUsed:
    """When each key was last used (T3), so admins can find keys nothing uses any more.

    A write per request would be a Postgres UPDATE on the hot path, and row-lock contention on busy keys. Instead
    each copy remembers the latest time per key in memory and writes them all in one statement a minute. The
    column is accurate to about a minute, which is all "is this key still used?" needs.
    """

    def __init__(self, source, every: float = 60.0, clock: Callable[[], dt.datetime] = None):
        self.source = source
        self.every = every
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.seen: Dict[int, dt.datetime] = {}
        self._task: Optional[asyncio.Task] = None

    def saw(self, key_id: int) -> None:
        self.seen[key_id] = self.clock()

    async def flush(self) -> None:
        if not self.seen:
            return
        batch, self.seen = self.seen, {}
        try:
            await self.source.touch(batch)
        except (OSError, asyncio.TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError) as exc:
            log.warning("couldn't record when %d keys were last used, will retry: %r", len(batch), exc)
            for key_id, at in batch.items():  # keep them for the next flush, unless a newer time came in meanwhile
                self.seen[key_id] = max(at, self.seen.get(key_id, at))

    def start(self) -> None:
        async def loop():
            while True:
                await asyncio.sleep(self.every)
                await self.flush()
        self._task = asyncio.create_task(loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        await self.flush()

