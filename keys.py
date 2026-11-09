"""Team API keys. Decisions: DECISIONS.md section J.

A key looks like sbk_ + 32 random characters. It is shown once, when created. Postgres stores only its
SHA-256 hash, plus a short visible prefix so people can tell keys apart. Each gateway copy keeps recent
answers in memory: valid for 60 s, invalid for 10 s. Only a cache miss goes to Postgres.
"""
import asyncio
import hashlib
import secrets
import string
import time
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import asyncpg

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
"""


@dataclass(frozen=True)
class KeyInfo:
    id: int
    team: str
    prefix: str


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
    """The source of truth."""

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def find(self, key_hash: str) -> Optional[KeyInfo]:
        row = await self.pool.fetchrow(
            "SELECT id, team, prefix FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL", key_hash
        )
        return KeyInfo(row["id"], row["team"], row["prefix"]) if row else None

    async def create(self, team: str, name: str = "") -> Tuple[KeyInfo, str]:
        key = generate_key()
        row = await self.pool.fetchrow(
            "INSERT INTO api_keys (key_hash, prefix, team, name) VALUES ($1, $2, $3, $4) RETURNING id",
            hash_key(key), key[:VISIBLE_CHARS], team, name,
        )
        return KeyInfo(row["id"], team, key[:VISIBLE_CHARS]), key  # the only time the full key exists

    async def revoke(self, key_id: int) -> bool:
        result = await self.pool.execute(
            "UPDATE api_keys SET revoked_at = now() WHERE id = $1 AND revoked_at IS NULL", key_id
        )
        return result == "UPDATE 1"

    async def list(self) -> List[asyncpg.Record]:
        return await self.pool.fetch(
            "SELECT id, prefix, team, name, created_at, revoked_at FROM api_keys ORDER BY id"
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
