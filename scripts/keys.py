"""Create, list and revoke team API keys. A stand-in for the admin dashboard.

  .venv/bin/python scripts/keys.py create search "prod server"
  .venv/bin/python scripts/keys.py list
  .venv/bin/python scripts/keys.py revoke 7
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

import keys  # noqa: E402


async def main(args):
    load_dotenv()
    store = await keys.connect(os.environ["DATABASE_URL"])
    try:
        if args[:1] == ["create"] and len(args) in (2, 3):
            info, key = await store.create(team=args[1], name=args[2] if len(args) == 3 else "")
            print(f"created key {info.id} for team {info.team!r}:\n\n  {key}\n")
            print("Copy it now. It is not stored anywhere and can't be shown again.")
        elif args == ["list"]:
            for r in await store.list():
                status = f"revoked {r['revoked_at']:%Y-%m-%d %H:%M}" if r["revoked_at"] else "active"
                print(f"{r['id']:>4}  {r['prefix']}...  {r['team']:<12} {r['name']:<20} {status}")
        elif args[:1] == ["revoke"] and len(args) == 2:
            ok = await store.revoke(int(args[1]))
            print(f"key {args[1]} revoked; gateways stop accepting it within 60 s" if ok
                  else f"no active key with id {args[1]}")
        else:
            print(__doc__)
            sys.exit(1)
    finally:
        await store.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))
