#!/usr/bin/env bash
# The chaos test (DECISIONS.md R): steady traffic to model `mock-ha` (mock, falling back to mock-b) for 3 minutes.
# After 60 s, `mock` breaks; after 120 s it recovers. Then chaos_report.py prints what clients saw.
#   loadtest/chaos.sh kill      # mock's container is killed (SIGKILL), then started again
#   loadtest/chaos.sh errors    # mock answers every request with a 500
#   loadtest/chaos.sh slow      # mock takes 20 s to the first token
#   STREAM=1 RATE=50 loadtest/chaos.sh kill
#
# Same setup as run.sh (the load-test stack and LOADTEST_KEY).
set -euo pipefail
cd "$(dirname "$0")/.."

scenario=${1:?scenario: kill, errors or slow}
rate=${RATE:-100}
stream=${STREAM:-0}
compose="docker compose -f docker-compose.yml -f loadtest/docker-compose.yml"
name="chaos-$scenario-$([ "$stream" = 1 ] && echo stream || echo plain)-$rate"
log=loadtest/results/$name.log
events=loadtest/results/$name.events
control=loadtest/results/mock.json  # the mock reads this as /state/mock.json

mkdir -p loadtest/results
rm -f "$log" "$control"
: > "$events"
now() { python3 -c 'import time; print(f"{time.time():.3f}")'; }
event() { echo "$(now) $1" >> "$events"; echo "$(date +%T) $1"; }

KEY=${LOADTEST_KEY:?export LOADTEST_KEY first} \
$compose run --rm --quiet-pull -e KEY -e STREAM="$stream" -e RATE="$rate" -e DURATION=180s \
  k6 run --quiet --log-format raw --log-output "file=results/$name.log" chaos.js &
k6_pid=$!

until [ -s "$log" ]; do  # traffic has started
  kill -0 $k6_pid 2>/dev/null || { echo "k6 exited before sending anything"; exit 1; }
  sleep 1
done
event start
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
sleep 60

event break
case $scenario in
  kill) docker kill "$($compose ps -q mock)" > /dev/null ;;
  errors) echo '{"error_rate": 1}' > "$control" ;;
  slow) echo '{"ttft_ms": 20000}' > "$control" ;;
  *) echo "unknown scenario $scenario"; kill $k6_pid; exit 1 ;;
esac
sleep 60

event recover
case $scenario in
  kill) $compose start mock > /dev/null ;;
  *) rm -f "$control" ;;
esac

wait $k6_pid
event end
$compose logs --no-log-prefix --since "$started" gateway | grep -i breaker | sed 's/^/  gateway: /' || true
python3 loadtest/chaos_report.py "$log" "$events" | tee -a loadtest/results/chaos.txt
