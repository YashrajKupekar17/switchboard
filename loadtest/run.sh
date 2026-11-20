#!/usr/bin/env bash
# Run one load test and append its numbers to loadtest/results/runs.jsonl (DECISIONS.md Q).
#   loadtest/run.sh gateway 50          # through the gateway, 50 requests per second, for 60 s
#   STREAM=1 loadtest/run.sh mock 200   # straight to the mock, streamed
#   DURATION=30s loadtest/run.sh cache 100
#
# Setup, once:
#   docker compose -f docker-compose.yml -f loadtest/docker-compose.yml up -d --build   # 1 gateway copy + the mock
#   .venv/bin/python scripts/keys.py create loadtest k6         # then export LOADTEST_KEY=<the key>
#   .venv/bin/python scripts/keys.py create loadtest-cache k6   # then export LOADTEST_CACHE_KEY=<the key>
# Afterwards, `docker compose up -d` (without the second -f) puts the normal 3 copies back. Revoke the keys.
#
# The keys come from env vars, never from the command line: LOADTEST_KEY (team loadtest) and
# LOADTEST_CACHE_KEY (team loadtest-cache, for the cache target).
set -euo pipefail
cd "$(dirname "$0")/.."

target=${1:?target: mock, gateway or cache}
rate=${2:?requests per second}
mkdir -p loadtest/results

KEY=${LOADTEST_KEY:-} CACHE_KEY=${LOADTEST_CACHE_KEY:-} \
docker compose -f docker-compose.yml -f loadtest/docker-compose.yml run --rm --quiet-pull \
  -e TARGET="$target" -e STREAM="${STREAM:-0}" -e RATE="$rate" -e DURATION="${DURATION:-60s}" -e KEY -e CACHE_KEY \
  k6 run --quiet chat.js | tail -1 | tee -a loadtest/results/runs.jsonl
