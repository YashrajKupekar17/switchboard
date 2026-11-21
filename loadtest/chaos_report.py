"""What clients saw during a chaos run (chaos.sh): outcomes in 5-second steps, then per phase.

  python3 loadtest/chaos_report.py loadtest/results/chaos-kill-plain-100.log loadtest/results/chaos-kill-plain-100.events

Requests are placed by when they were sent, relative to the moment `mock` broke.
"""
import json
import os
import sys
from collections import Counter

OUTCOMES = ["ok", "fallback", "shed", "failed"]
STEP = 5  # seconds


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(p / 100 * len(values)))]


def main(log_path, events_path):
    events = {}
    with open(events_path) as f:
        for line in f:
            t, name = line.split()
            events[name] = float(t)
    with open(log_path) as f:
        reqs = [json.loads(line) for line in f if line.startswith("{")]

    broke, recovered = events["break"], events["recover"]

    def phase(t):
        return "before" if t < broke else "broken" if t < recovered else "after"

    print(f"\n{log_path}: {len(reqs)} requests")
    print(f"{'t (s)':>6} {'sent':>5} " + " ".join(f"{o:>8}" for o in OUTCOMES) + f" {'p50 ms':>7} {'p99 ms':>7}")
    steps = {}
    for r in reqs:
        steps.setdefault(int((r["t"] - broke) // STEP) * STEP, []).append(r)
    for start in sorted(steps):
        rs = steps[start]
        counts = Counter(r["outcome"] for r in rs)
        served = [r["ms"] for r in rs if r["outcome"] in ("ok", "fallback")]
        mark = "  <- break" if start == 0 else "  <- recover" if start == int((recovered - broke) // STEP) * STEP else ""
        print(f"{start:>6} {len(rs):>5} " + " ".join(f"{counts[o]:>8}" for o in OUTCOMES)
              + f" {pct(served, 50) or '-':>7} {pct(served, 99) or '-':>7}{mark}")

    summary = {"run": os.path.basename(log_path)[:-len(".log")]}
    for name in ("before", "broken", "after"):
        rs = [r for r in reqs if phase(r["t"]) == name]
        counts = Counter(r["outcome"] for r in rs)
        served = [r["ms"] for r in rs if r["outcome"] in ("ok", "fallback")]
        summary[name] = {
            "sent": len(rs),
            "success_pct": round(100 * (counts["ok"] + counts["fallback"]) / len(rs), 2) if rs else None,
            **{o: counts[o] for o in OUTCOMES},
            "failed_statuses": dict(Counter(str(r["status"]) for r in rs if r["outcome"] == "failed")),
            "p50_ms": pct(served, 50),
            "p99_ms": pct(served, 99),
        }
    print(json.dumps(summary))


if __name__ == "__main__":
    main(*sys.argv[1:3])
