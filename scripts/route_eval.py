"""Is the cheap model's answer good enough? Per team and per rule, from route shadow mode (DECISIONS.md P5).

  .venv/bin/python scripts/route_eval.py export search > route_shadow.jsonl
  .venv/bin/python scripts/route_eval.py judge route_shadow.jsonl > judged.jsonl
  .venv/bin/python scripts/route_eval.py table judged.jsonl [--good 0.9]

`export` prints a team's shadow records from redis-cache: the conversation, the strong and the cheap model's answers,
which one was served, and which rule picked it.

`judge` asks a judge model to compare the two answers, twice with the order swapped, and fills in "verdict":
cheap_as_good, strong_better or unsure. Records that already have a verdict (yours, if you edited one) are kept.

`table` prints, for each rule, how often the cheap answer was as good:
  - for upgrades (tools, turns, long_prompt), a high share means the rule spends money for nothing on this traffic;
  - for the default, a high "strong better" share means hard requests that no rule catches.
"""
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import redis.asyncio as redis  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import router  # noqa: E402


def ask_judge(record, first, second):
    prompt = router.JUDGE_PROMPT.format(conversation=router.render_conversation(record["messages"]),
                                        first=router.render_answer(first), second=router.render_answer(second))
    response = httpx.post(
        "https://api.openai.com/v1/chat/completions",
        json={"model": router.JUDGE_MODEL, "temperature": 0, "max_tokens": 3,
              "messages": [{"role": "user", "content": prompt}]},
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=60,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"].strip().lower().rstrip(".")


def judge(path):
    for line in open(path):
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("verdict") is None:
            strong_first = ask_judge(record, record["strong_answer"], record["cheap_answer"])
            cheap_first = ask_judge(record, record["cheap_answer"], record["strong_answer"])
            record["verdict"] = router.verdict(strong_first, cheap_first)
            record["judge_said"] = [strong_first, cheap_first]
        print(json.dumps(record, ensure_ascii=False), flush=True)


def table(path, good):
    records = [json.loads(line) for line in open(path) if line.strip()]
    missing = [r for r in records if r.get("verdict") is None]
    if missing:
        sys.exit(f"{len(missing)} records have no verdict: run `judge` first")
    groups = defaultdict(list)
    for r in records:
        groups[(r["team"], r["reason"])].append(r["verdict"])

    print("team          rule          served   n   cheap as good   strong better   unsure")
    advice = []
    for (team, reason), verdicts in sorted(groups.items()):
        n = len(verdicts)
        share = {v: verdicts.count(v) / n for v in ("cheap_as_good", "strong_better", "unsure")}
        served = "cheap " if reason == "default" else "strong"
        print(f"{team:<13} {reason:<13} {served}  {n:4}   {share['cheap_as_good']:12.0%}   "
              f"{share['strong_better']:12.0%}   {share['unsure']:6.0%}")
        if reason != "default" and share["cheap_as_good"] >= good:
            advice.append(f"{team}: the {reason!r} rule upgraded requests the cheap model answered as well "
                          f"{share['cheap_as_good']:.0%} of the time. Loosen or turn off that rule for this team.")
        if reason == "default" and share["strong_better"] > 1 - good:
            advice.append(f"{team}: the strong model was better on {share['strong_better']:.0%} of requests no rule "
                          f"upgraded. Read those records: what do they have in common?")
    print(f"\n{len(records)} records. 'unsure' = the judge changed its mind when the order was swapped.")
    if any(n < 30 for n in map(len, groups.values())):
        print("Rules with fewer than ~30 records: too few to act on yet.")
    for line in advice:
        print("- " + line)


async def export(team):
    client = redis.from_url(os.environ.get("CACHE_REDIS_URL", "redis://localhost:6380/0"))
    try:
        for record in await router.ShadowLog(client).records(team):
            print(json.dumps(record, ensure_ascii=False))
    finally:
        await client.aclose()


def main(args):
    load_dotenv()
    if args[:1] == ["export"] and len(args) == 2:
        asyncio.run(export(args[1]))
    elif args[:1] == ["judge"] and len(args) == 2:
        judge(args[1])
    elif args[:1] == ["table"] and len(args) >= 2:
        options = args[2:]
        table(args[1], float(options[options.index("--good") + 1]) if "--good" in options else 0.9)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
