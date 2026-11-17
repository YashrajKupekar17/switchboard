"""Pick a semantic cache threshold from labelled pairs (DECISIONS.md O2).

  .venv/bin/python scripts/semantic_eval.py table evals/semantic_pairs.jsonl [--max-wrong 0.01]
  .venv/bin/python scripts/semantic_eval.py export search > shadow.jsonl

`table` reads pairs, one JSON per line: {"question", "matched_question", "same": true|false}. Pairs without a
"similarity" are embedded with the same model the gateway uses. It prints, for each threshold, how many pairs would
be served as hits and how many of those are wrong, then the lowest threshold whose wrong share stays under the limit.

`export` prints a team's shadow-mode pairs (would-be hits, with both answers) from redis-cache, for labelling:
fill in "same" on each line, then run `table` on the file.
"""
import asyncio
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
import redis.asyncio as redis  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

import semantic  # noqa: E402

THRESHOLDS = (0.70, 0.75, 0.80, 0.85, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99)


def embed(texts):
    response = httpx.post(
        "https://api.openai.com/v1/embeddings", json={"model": semantic.EMBED_MODEL, "input": texts},
        headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, timeout=30,
    )
    response.raise_for_status()
    return [d["embedding"] for d in sorted(response.json()["data"], key=lambda d: d["index"])]


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def table(path, max_wrong):
    pairs = [json.loads(line) for line in open(path) if line.strip()]
    unlabelled = [p for p in pairs if p.get("same") is None]
    if unlabelled:
        sys.exit(f"{len(unlabelled)} pairs have no label: set \"same\" to true or false first")
    todo = [p for p in pairs if "similarity" not in p]
    if todo:
        vectors = embed([t for p in todo for t in (p["question"], p["matched_question"])])
        for i, p in enumerate(todo):
            p["similarity"] = cosine(vectors[2 * i], vectors[2 * i + 1])

    repeats = sum(p["same"] for p in pairs)
    print(f"{len(pairs)} pairs: {repeats} the same question, {len(pairs) - repeats} different "
          f"(embedding model: {semantic.EMBED_MODEL})\n")
    print("threshold  served  wrong  precision  recall")
    best = None
    for t in THRESHOLDS:
        served = [p for p in pairs if p["similarity"] >= t]
        wrong = sum(not p["same"] for p in served)
        right = len(served) - wrong
        precision = f"{right / len(served):9.0%}" if served else "        -"
        print(f"  {t:.2f}     {len(served):5}  {wrong:5}  {precision}  {right / repeats if repeats else 0:6.0%}")
        if best is None and served and wrong / len(served) <= max_wrong:
            best = t
    print("\n  served = pairs that would be a hit; precision = served hits that were right;"
          "\n  recall = real repeats that were caught\n")
    if best is None:
        print(f"No threshold keeps wrong hits at or under {max_wrong:.0%}. Leave the semantic cache off for this traffic.")
    else:
        print(f"Lowest threshold with at most {max_wrong:.0%} wrong: {best:.2f}")

    print("\nEvery pair, highest score first:")
    for p in sorted(pairs, key=lambda p: -p["similarity"]):
        mark = "same" if p["same"] else "DIFF"
        print(f"  {p['similarity']:.3f}  {mark}  {p['question']!r}  vs  {p['matched_question']!r}")


async def export(team):
    client = redis.from_url(os.environ.get("CACHE_REDIS_URL", "redis://localhost:6380/0"))
    try:
        for record in await semantic.RedisVectors(client).shadow_records(team):
            print(json.dumps(record, ensure_ascii=False))
    finally:
        await client.aclose()


def main(args):
    load_dotenv()
    if args[:1] == ["table"] and len(args) in (2, 4):
        max_wrong = float(args[3]) if len(args) == 4 and args[2] == "--max-wrong" else 0.01
        table(args[1], max_wrong)
    elif args[:1] == ["export"] and len(args) == 2:
        asyncio.run(export(args[1]))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
