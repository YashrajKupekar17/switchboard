"""Create the tables and the gateway's restricted database user. Safe to run again.

Runs as the database owner (DATABASE_URL). The gateway itself logs in as `gateway`, which can only
read keys and add usage rows (DECISIONS.md M3), so a hacked gateway can't create keys for itself.

  .venv/bin/python scripts/setup_db.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import budgets  # noqa: E402
import keys  # noqa: E402

GRANTS = """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'gateway') THEN CREATE ROLE gateway LOGIN; END IF;
END $$;
GRANT SELECT ON api_keys TO gateway;                          -- check keys; never create or revoke them
GRANT SELECT, INSERT ON usage_ledger TO gateway;              -- add rows and sum them; never change or delete them
GRANT USAGE ON SEQUENCE usage_ledger_id_seq TO gateway;       -- needed to INSERT into a BIGSERIAL table
"""


async def main():
    load_dotenv()
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():  # all or nothing
            await conn.execute(keys.SCHEMA)
            await conn.execute(budgets.SCHEMA)
            await conn.execute(GRANTS)
            # DDL can't take $1 parameters, so quote the password with Postgres's own format(%L).
            password = os.environ.get("GATEWAY_DB_PASSWORD", "gateway")
            await conn.execute(await conn.fetchval("SELECT format('ALTER ROLE gateway PASSWORD %L', $1::text)", password))
        print("tables and the gateway user are ready")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
