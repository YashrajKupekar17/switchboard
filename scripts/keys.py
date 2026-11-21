"""Create, list, rotate, change and revoke team API keys. A stand-in for an admin dashboard (DECISIONS.md J, T).

  .venv/bin/python scripts/keys.py create search "prod server" --expires-in-days 90 --max-usd 200 --rpm 300
  .venv/bin/python scripts/keys.py list [--team search]
  .venv/bin/python scripts/keys.py rotate 7 --grace-hours 24
  .venv/bin/python scripts/keys.py set 7 --max-usd 500 --tpm none
  .venv/bin/python scripts/keys.py revoke 7 --reason "left in a public repo"
  .venv/bin/python scripts/keys.py audit [--key 7]

Runs as the database owner (DATABASE_URL). Every change is recorded in key_audit, with the person running it
(SWITCHBOARD_ADMIN, or the login name). Changes reach every gateway copy within a minute (each copy's key cache).
A key's own limits and budget apply on top of its team's, so a key can be tighter than its team, never looser.
"""
import argparse
import asyncio
import datetime as dt
import getpass
import os
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

import keys  # noqa: E402
from pricing import usd  # noqa: E402


def whole(text):
    """A positive whole number, or 'none' to clear the setting."""
    if text == "none":
        return None
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1, or 'none'")
    return value


def dollars(text):
    if text == "none":
        return None
    value = Decimal(text)
    if value < 0:
        raise argparse.ArgumentTypeError("can't be negative")
    return value


def expiry(text):
    """Days from now, or 'none' for a key that never expires."""
    return None if text == "none" else dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=whole(text))


def settings(args) -> dict:
    """Only the options that were given: an option left out leaves the setting as it is. (With SUPPRESS as the
    default, argparse doesn't set an option that wasn't given at all.)"""
    options = {"rpm": "rpm", "tpm": "tpm", "max_usd": "max_usd", "expires_at": "expires_in_days"}
    return {setting: getattr(args, option) for setting, option in options.items() if hasattr(args, option)}


def status(row, now) -> str:
    if row["revoked_at"]:
        return f"revoked {row['revoked_at']:%Y-%m-%d}"
    if row["expires_at"] and row["expires_at"] <= now:
        return f"expired {row['expires_at']:%Y-%m-%d}"
    if row["expires_at"]:
        return f"expires {row['expires_at']:%Y-%m-%d}"
    return "active"


def ago(when, now) -> str:
    if when is None:
        return "never"
    minutes = int((now - when).total_seconds() // 60)
    return (f"{minutes}m ago" if minutes < 60 else f"{minutes // 60}h ago" if minutes < 48 * 60
            else f"{minutes // 1440}d ago")


def limits(row) -> str:
    parts = [f"{row['rpm']} rpm" if row["rpm"] else "", f"{row['tpm']} tpm" if row["tpm"] else "",
             f"{usd(float(row['max_usd']))}/month" if row["max_usd"] is not None else ""]
    return ", ".join(p for p in parts if p) or "team's"


def show_new(info, key):
    print(f"key {info.id} for team {info.team!r}:\n\n  {key}\n")
    print("Copy it now. It is not stored anywhere and can't be shown again.")


async def main(args):
    load_dotenv()
    store = await keys.connect(os.environ["DATABASE_URL"])
    actor = os.environ.get("SWITCHBOARD_ADMIN") or getpass.getuser()
    now = dt.datetime.now(dt.timezone.utc)
    try:
        if args.command == "create":
            show_new(*await store.create(args.team, args.name, actor=actor, **settings(args)))

        elif args.command == "list":
            rows = await store.list(args.team)
            table = [(str(r["id"]), f"{r['prefix']}...", r["team"], r["name"], status(r, now),
                      ago(r["last_used_at"], now), limits(r)) for r in rows]
            headers = ("id", "key", "team", "name", "status", "last used", "own limits")
            widths = [max([len(h), *(len(row[i]) for row in table)]) for i, h in enumerate(headers)]
            for row in [headers, *table]:
                print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))

        elif args.command == "rotate":
            info, key = await store.rotate(args.id, dt.timedelta(hours=args.grace_hours), actor=actor)
            show_new(info, key)
            print(f"Key {args.id} keeps working for {args.grace_hours} h, then expires. Move every service to the "
                  f"new key before then; `list` shows when the old one was last used.")

        elif args.command == "set":
            changes = settings(args)
            if not changes:
                sys.exit("nothing to change: give --rpm, --tpm, --max-usd or --expires-in-days")
            ok = await store.update(args.id, actor=actor, **changes)
            print(f"key {args.id} updated; gateways use the new settings within 60 s" if ok
                  else f"no active key with id {args.id}")

        elif args.command == "revoke":
            ok = await store.revoke(args.id, actor=actor, reason=args.reason)
            print(f"key {args.id} revoked; gateways stop accepting it within 60 s" if ok
                  else f"no active key with id {args.id}")

        elif args.command == "audit":
            for r in await store.audit(args.key, args.limit):
                print(f"{r['at']:%Y-%m-%d %H:%M}  {r['actor']:<12} {r['action']:<7} key {r['key_id']:<5} {r['details']}")
    except LookupError as exc:
        sys.exit(str(exc))
    finally:
        await store.close()


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = p.add_subparsers(dest="command", required=True)

    def key_settings(cmd):
        # SUPPRESS as the default, so `set` can tell "not given" (leave it) from "none" (clear it).
        cmd.add_argument("--rpm", type=whole, default=argparse.SUPPRESS, help="requests per minute, or none")
        cmd.add_argument("--tpm", type=whole, default=argparse.SUPPRESS, help="tokens per minute, or none")
        cmd.add_argument("--max-usd", type=dollars, default=argparse.SUPPRESS, help="monthly budget, or none")
        cmd.add_argument("--expires-in-days", type=expiry, default=argparse.SUPPRESS, help="days, or none")

    create = commands.add_parser("create", help="a new key for a team")
    create.add_argument("team")
    create.add_argument("name", nargs="?", default="", help="what uses it, e.g. 'prod server'")
    key_settings(create)

    listing = commands.add_parser("list", help="every key, with status, last use and its own limits")
    listing.add_argument("--team")

    rotate = commands.add_parser("rotate", help="a new key with the same settings; the old one expires later")
    rotate.add_argument("id", type=int)
    rotate.add_argument("--grace-hours", type=float, default=24, help="how long the old key keeps working")

    change = commands.add_parser("set", help="change a key's own limits, budget or expiry")
    change.add_argument("id", type=int)
    key_settings(change)

    revoke = commands.add_parser("revoke", help="stop a key now")
    revoke.add_argument("id", type=int)
    revoke.add_argument("--reason", default="")

    audit = commands.add_parser("audit", help="who changed which key, newest first")
    audit.add_argument("--key", type=int)
    audit.add_argument("--limit", type=int, default=50)
    return p


if __name__ == "__main__":
    asyncio.run(main(parser().parse_args()))
