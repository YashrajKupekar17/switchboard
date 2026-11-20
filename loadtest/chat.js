// The load test (DECISIONS.md Q). Requests arrive at a fixed rate whether or not earlier ones have finished
// (open-loop), so a slow gateway can't make k6 send less and hide its own slowness.
//
//   TARGET    mock = straight to the mock (the baseline), gateway = through the gateway,
//             cache = through the gateway, every request an exact cache hit
//   STREAM    1 to stream the answers
//   RATE      requests per second          DURATION  e.g. "60s"
//
// first_byte_ms is time to first byte: for a stream that's the first token, since the gateway (and the mock)
// only send headers once it's in. total_ms is the whole answer.
import http from "k6/http";
import { check } from "k6";
import { Counter, Trend } from "k6/metrics";

// Latency of answered requests only. With shedding on, the fast 503s would pull k6's own percentiles down and
// make overload look fast. A shed request (503 gateway_overloaded) is refused on purpose, so it isn't a failure.
const answered = new Trend("answered_ms", true);
const answeredFirstByte = new Trend("answered_first_byte_ms", true);
const shed = new Counter("shed");

const TARGET = __ENV.TARGET || "gateway";
const STREAM = __ENV.STREAM === "1";
const RATE = Number(__ENV.RATE || 10);
const DURATION = __ENV.DURATION || "60s";

const TARGETS = {
  mock: { url: "http://mock:9000/v1/chat/completions", key: "" },
  gateway: { url: "http://gateway:8000/v1/chat/completions", key: __ENV.KEY },
  cache: { url: "http://gateway:8000/v1/chat/completions", key: __ENV.CACHE_KEY },
};
const target = TARGETS[TARGET];

// About 250 tokens of prompt: a system message and a question, like a small chat app. The same for every
// request, which only matters for the cache (that's what makes every request a hit).
const body = JSON.stringify({
  model: "mock",
  stream: STREAM,
  messages: [
    { role: "system", content: "You are a helpful assistant for an online store. ".repeat(12) },
    { role: "user", content: "What is your return policy for shoes bought on sale? ".repeat(8) },
  ],
});

export const options = {
  scenarios: {
    load: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      // Each request takes ~1.1 s, so ~1.1 x RATE are in flight. If k6 runs out of these it skips requests and
      // counts them in dropped_iterations: a run with any dropped is not a valid open-loop run.
      preAllocatedVUs: Math.ceil(RATE * 2),
      maxVUs: Math.ceil(RATE * 20),
    },
  },
  summaryTrendStats: ["p(50)", "p(95)", "p(99)", "max"],
};

const headers = { "Content-Type": "application/json" };
if (target.key) headers.Authorization = `Bearer ${target.key}`;

// Runs once before the load starts. For the cache it stores the answer, so the run measures hits only.
export function setup() {
  http.post(target.url, body, { headers, timeout: "30s" });
}

export default function () {
  const res = http.post(target.url, body, { headers, timeout: "30s" });
  if (res.status === 200) {
    answered.add(res.timings.duration);
    answeredFirstByte.add(res.timings.waiting);
  } else if (res.status === 503 && res.body && res.body.includes("gateway_overloaded")) {
    shed.add(1);
    return;
  }
  check(res, { "status 200": (r) => r.status === 200 });
}

function ms(v) {
  return v === undefined ? null : Math.round(v * 10) / 10;
}

// One line of JSON per run, appended to results/runs.jsonl by run.sh: the numbers the results table is built from.
export function handleSummary(data) {
  const m = data.metrics;
  const trend = (name) => {
    const v = m[name] ? m[name].values : {};
    return { p50: ms(v["p(50)"]), p95: ms(v["p(95)"]), p99: ms(v["p(99)"]), max: ms(v.max) };
  };
  const row = {
    target: TARGET,
    stream: STREAM,
    rate: RATE,
    duration: DURATION,
    sent: m.http_reqs ? m.http_reqs.values.count : 0,
    achieved_rps: m.http_reqs ? Math.round(m.http_reqs.values.rate * 10) / 10 : 0,
    failed: m.checks ? m.checks.values.fails : 0,
    shed: m.shed ? m.shed.values.count : 0,
    dropped: m.dropped_iterations ? m.dropped_iterations.values.count : 0,
    first_byte_ms: trend("answered_first_byte_ms"),
    total_ms: trend("answered_ms"),
  };
  return { stdout: JSON.stringify(row) + "\n" };
}
