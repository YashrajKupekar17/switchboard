// The chaos test (chaos.sh). Steady open-loop traffic to model `mock-ha` (mock first, mock-b as fallback) while
// chaos.sh breaks `mock`. Logs one line per request, so chaos_report.py can show what happened second by second.
//
//   STREAM 1 to stream     RATE requests per second     DURATION e.g. "180s"
import http from "k6/http";

const STREAM = __ENV.STREAM === "1";
const RATE = Number(__ENV.RATE || 100);
const DURATION = __ENV.DURATION || "180s";

const URL = "http://gateway:8000/v1/chat/completions";
const headers = { "Content-Type": "application/json", Authorization: `Bearer ${__ENV.KEY}` };
const body = JSON.stringify({
  model: "mock-ha",
  stream: STREAM,
  messages: [
    { role: "system", content: "You are a helpful assistant for an online store. ".repeat(12) },
    { role: "user", content: "What is your return policy for shoes bought on sale? ".repeat(8) },
  ],
});

export const options = {
  scenarios: {
    chaos: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      preAllocatedVUs: RATE * 2,
      maxVUs: RATE * 40, // a broken provider makes requests slow, so far more are in flight than in the load test
    },
  },
};

// ok        200, served by the first choice
// fallback  200, served by mock-b after mock failed (x-gateway-attempts lists what failed)
// shed      503 gateway_overloaded: refused on purpose
// failed    anything else, including a stream that broke after it started (200, but no [DONE])
function outcome(res) {
  if (res.status === 200) {
    if (STREAM && !(res.body || "").includes("data: [DONE]")) return "failed";
    return res.headers["X-Gateway-Attempts"] ? "fallback" : "ok";
  }
  if (res.status === 503 && (res.body || "").includes("gateway_overloaded")) return "shed";
  return "failed";
}

export default function () {
  const sent = Date.now() / 1000;
  const res = http.post(URL, body, { headers, timeout: "30s" });
  console.log(JSON.stringify({
    t: Math.round(sent * 1000) / 1000,
    outcome: outcome(res),
    status: res.status, // 0 = no response at all (k6 timed out after 30 s)
    ms: Math.round(res.timings.duration),
  }));
}

export function handleSummary() {
  return {}; // chaos_report.py builds the summary from the log
}
