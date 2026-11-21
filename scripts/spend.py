"""Spend report from the usage ledger: who spent what, on what (DECISIONS.md T1). Showback, not billing.

  .venv/bin/python scripts/spend.py                                  # this month, by team
  .venv/bin/python scripts/spend.py --by team --by tag:feature       # each team's spend, split by feature
  .venv/bin/python scripts/spend.py --team search --by key --month 2026-10

--by takes team, key, model (what the client asked for), deployment (what answered), or tag:<name> for a
request tag (x-gateway-tags); requests without that tag show as "(none)". Runs as the database owner (DATABASE_URL).
"""
import argparse
import asyncio
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import tags  # noqa: E402

COLUMNS = {  # --by name -> SQL. Only these, or a tag: never text from the command line in the query.
    "team": "l.team",
    "key": "coalesce(k.prefix || ' ' || nullif(k.name, ''), k.prefix, l.key_id::text)",
    "model": "l.model",
    "deployment": "l.deployment",
}


def build_query(by, month_start, team=None):
    """(sql, params) for spend grouped by `by`, from month_start for one month."""
    params = [month_start, (month_start + dt.timedelta(days=32)).replace(day=1)]
    groups = []
    for name in by:
        if name.startswith("tag:"):
            tag = name[len("tag:"):]
            if not tags.NAME.fullmatch(tag):
                raise ValueError(f"not a valid tag name: {tag!r}")
            params.append(tag)
            groups.append(f"coalesce(l.tags ->> ${len(params)}, '(none)')")
        elif name in COLUMNS:
            groups.append(COLUMNS[name])
        else:
            raise ValueError(f"--by must be one of {', '.join(COLUMNS)} or tag:<name>, got {name!r}")
    where = "l.created_at >= $1 AND l.created_at < $2"
    if team:
        params.append(team)
        where += f" AND l.team = ${len(params)}"
    selected = ", ".join(f"{g} AS g{i}" for i, g in enumerate(groups))
    sql = (f"SELECT {selected}, count(*) AS requests, sum(l.cost_micros) AS cost, sum(l.saved_micros) AS saved, "
           f"count(*) FILTER (WHERE l.cache_hit) AS hits "
           f"FROM usage_ledger l LEFT JOIN api_keys k ON k.id = l.key_id WHERE {where} "
           f"GROUP BY {', '.join(f'g{i}' for i in range(len(groups)))} ORDER BY cost DESC")
    return sql, params


async def main(args):
    load_dotenv()
    year, month = map(int, args.month.split("-"))
    sql, params = build_query(args.by or ["team"], dt.datetime(year, month, 1, tzinfo=dt.timezone.utc), args.team)
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        rows = await conn.fetch(sql, *params)
    finally:
        await conn.close()

    headers = [*(args.by or ["team"]), "requests", "cost", "saved by cache", "cache hits"]
    table = [[*(str(r[f"g{i}"]) for i in range(len(headers) - 4)), str(r["requests"]), f"${r['cost'] / 1e6:,.4f}",
              f"${r['saved'] / 1e6:,.4f}", f"{100 * r['hits'] / r['requests']:.0f}%"] for r in rows]
    total_cost = sum(r["cost"] for r in rows)
    widths = [max(len(h), *(len(row[i]) for row in table)) if table else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    for row in table:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))
    print(f"\n{args.month}: ${total_cost / 1e6:,.4f} across {sum(r['requests'] for r in rows)} requests")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--by", action="append", help="team, key, model, deployment, or tag:<name>; repeatable")
    parser.add_argument("--team", help="only this team")
    parser.add_argument("--month", default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m"), help="YYYY-MM (UTC)")
    try:
        asyncio.run(main(parser.parse_args()))
    except ValueError as exc:
        parser.error(str(exc))
