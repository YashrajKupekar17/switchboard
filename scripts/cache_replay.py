"""What the cache saves on a replayed workload: a support bot's questions, sent through the gateway (DECISIONS.md S).

  .venv/bin/python scripts/cache_replay.py sample 1000 > notes/replay-sample.jsonl
  REPLAY_KEY=... .venv/bin/python scripts/cache_replay.py run notes/replay-sample.jsonl notes/replay-results.jsonl
  .venv/bin/python scripts/cache_replay.py report notes/replay-results.jsonl

`sample` picks questions at random (fixed seed) from Bitext's customer-support dataset: 26,872 questions a
customer might ask an online store, each labelled with one of 27 intents (CDLA-Sharing-1.0, from Hugging Face).

`run` sends them, in that order, as a support bot on gpt-4o-mini through the normal stack (http://localhost:8000),
with a key for team `replay`, which has the exact and semantic cache on (config.yaml). 4 at a time. It saves one
line per request, then the totals: the team's cost and savings from the usage ledger, and how many embedding and
judge calls the semantic cache made (from Prometheus), which the ledger doesn't record.

`report` prints hit rates, cost with and without the cache, and whether each semantic hit was right: a served
answer carries the id of the answer it was stored from, so we know which earlier question it answered, and the
hit is wrong if that question's intent was different.
"""
import asyncio
import csv
import io
import json
import os
import random
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg  # noqa: E402
import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()  # before config: it reads the providers' key variables when it loads

import semantic  # noqa: E402
from config import load_config  # noqa: E402

DATASET = ("https://huggingface.co/datasets/bitext/Bitext-customer-support-llm-chatbot-training-dataset/resolve/main/"
           "Bitext_Sample_Customer_Support_Training_Dataset_27K_responses-v11.csv")
SEED = 17
GATEWAY = "http://localhost:8000/v1/chat/completions"
PROMETHEUS = "http://localhost:9090/api/v1/query"
CONCURRENCY = 4
SYSTEM = ("You are the customer support assistant for an online store. Answer the customer's message helpfully "
          "in under 100 words. Placeholders like {{Order Number}} stand for the customer's real details.")
MAX_TOKENS = 200
EMBED_USD_PER_M = 0.02  # text-embedding-3-small, OpenAI's pricing page, 2026-11-09


def sample(n):
    rows = list(csv.DictReader(io.StringIO(httpx.get(DATASET, follow_redirects=True, timeout=60).text)))
    for row in random.Random(SEED).sample(rows, n):
        print(json.dumps({"question": row["instruction"], "intent": row["intent"]}))


def semantic_calls():
    """Embedding and judge calls so far, summed over the gateway copies."""
    result = httpx.get(PROMETHEUS, params={"query": "sum by (step) (gateway_semantic_step_seconds_count)"}).json()
    return {r["metric"]["step"]: int(float(r["value"][1])) for r in result["data"]["result"]}


async def ask(http, key, i, item):
    started = time.perf_counter()
    response = await http.post(GATEWAY, headers={"Authorization": f"Bearer {key}"}, json={
        "model": "gpt-4o-mini", "temperature": 0, "max_tokens": MAX_TOKENS,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": item["question"]}],
    })
    body = response.json()
    return {
        "i": i, **item, "status": response.status_code, "seconds": round(time.perf_counter() - started, 3),
        "cache": response.headers.get("x-gateway-cache"),
        "similarity": response.headers.get("x-gateway-cache-similarity"),
        "judge": response.headers.get("x-gateway-cache-judge"),
        "id": body.get("id"),
    }


async def run(sample_path, out_path):
    key = os.environ["REPLAY_KEY"]
    items = [json.loads(line) for line in open(sample_path)]
    calls_before = semantic_calls()
    started = datetime.now(timezone.utc)
    rows = [None] * len(items)
    slots = asyncio.Semaphore(CONCURRENCY)

    async with httpx.AsyncClient(timeout=60) as http:
        async def one(i, item):
            async with slots:
                rows[i] = await ask(http, key, i, item)
                if (i + 1) % 100 == 0:
                    print(f"{i + 1}/{len(items)}", file=sys.stderr)

        await asyncio.gather(*(one(i, item) for i, item in enumerate(items)))

    await asyncio.sleep(20)  # the ledger writes every second; Prometheus reads the counters every 15 s
    calls_after = semantic_calls()
    db = await asyncpg.connect(os.environ["DATABASE_URL"])
    ledger = await db.fetchrow(
        "SELECT count(*) AS rows, coalesce(sum(cost_micros), 0) AS cost, coalesce(sum(saved_micros), 0) AS saved "
        "FROM usage_ledger WHERE team = 'replay' AND created_at >= $1", started)
    await db.close()

    with open(out_path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
        f.write(json.dumps({"totals": {
            # int(): Postgres sums come back as Decimal
            "ledger_rows": ledger["rows"], "cost_micros": int(ledger["cost"]), "saved_micros": int(ledger["saved"]),
            "embed_calls": calls_after.get("embed", 0) - calls_before.get("embed", 0),
            "judge_calls": calls_after.get("judge", 0) - calls_before.get("judge", 0),
        }}) + "\n")
    report(out_path)


def pct(values, p):
    values = sorted(values)
    return values[min(len(values) - 1, int(p / 100 * len(values)))] if values else None


def report(path):
    lines = [json.loads(line) for line in open(path)]
    rows, totals = lines[:-1], lines[-1]["totals"]
    n = len(rows)
    by_cache = Counter(r["cache"] for r in rows)
    print(f"{n} requests, {sum(r['status'] != 200 for r in rows)} not 200, {totals['ledger_rows']} ledger rows")
    for kind in ("miss", "hit", "semantic-hit"):
        seconds = [r["seconds"] for r in rows if r["cache"] == kind]
        print(f"  {kind:<13} {by_cache[kind]:>5} ({100 * by_cache[kind] / n:4.1f}%)  "
              f"p50 {pct(seconds, 50)} s  p95 {pct(seconds, 95)} s")

    # Was each semantic hit right? Its id is the id of the answer it was stored from: the first request that got
    # that id is the question it really answered.
    first_with_id = {}
    for r in sorted(rows, key=lambda r: r["i"]):
        first_with_id.setdefault(r["id"], r)
    semantic_hits = [r for r in rows if r["cache"] == "semantic-hit"]
    wrong = [(r, first_with_id[r["id"]]) for r in semantic_hits if first_with_id[r["id"]]["intent"] != r["intent"]]
    print(f"semantic hits answered with a different intent's answer: {len(wrong)} of {len(semantic_hits)}")
    for r, source in wrong:
        print(f"  {r['question']!r} ({r['intent']}) got the answer to {source['question']!r} ({source['intent']})")

    # Cost. The ledger has what the gateway paid providers for answers, and what hits would have cost. The
    # semantic cache's own calls aren't in it: price them here, by estimated tokens (4 characters per token).
    price = load_config(str(Path(__file__).resolve().parent.parent / "config.yaml")).prices["openai/gpt-4o-mini"]
    questions = sum(len(r["question"]) for r in rows) / n
    embed_usd = totals["embed_calls"] * (questions / 4) * EMBED_USD_PER_M / 1e6
    judge_tokens = (len(semantic.JUDGE_PROMPT) + 2 * questions) / 4
    judge_usd = totals["judge_calls"] * (judge_tokens * float(price.input) + float(price.output)) / 1e6
    answers_usd = totals["cost_micros"] / 1e6
    without_usd = answers_usd + totals["saved_micros"] / 1e6
    with_usd = answers_usd + embed_usd + judge_usd
    print(f"embedding calls {totals['embed_calls']} (${embed_usd:.4f}), judge calls {totals['judge_calls']} "
          f"(${judge_usd:.4f}), judge approved {by_cache['semantic-hit']} of them")
    print(f"cost without the cache ${without_usd:.4f}, with it ${with_usd:.4f}: "
          f"{100 * (1 - with_usd / without_usd):.1f}% saved")


if __name__ == "__main__":
    command, args = sys.argv[1], sys.argv[2:]
    if command == "sample":
        sample(int(args[0]))
    elif command == "run":
        asyncio.run(run(*args))
    elif command == "report":
        report(args[0])
    else:
        print(__doc__)
        sys.exit(1)
