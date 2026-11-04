# LLM Gateway — System Design (your working doc)

> Fill this in **before** you build each component. Write your answer, then we review it together.
> It's fine to be wrong at first. Revise the answer once you've measured.
> Each section ends with a **Decision** and a **Tradeoff** line. Those two lines are your interview answer.

---

## Problem statement (framework Steps 1–3)

**Context:** About 20 internal dev teams call LLM providers directly from their backend services. Today:
- (a) a provider outage took our support bot down for 2 hours;
- (b) nobody can tell which team is driving a bill that grew 4×;
- (c) one team's runaway script used up the shared rate limit and broke every other team.

**Problem:** Build an LLM gateway that takes a chat request plus a team API key, routes it to one of several providers (OpenAI, Anthropic, local Ollama), and returns the response.

**Success:**
- No client errors when any single provider is down
- 100% of requests attributed to a team
- LLM cost down 30% without hurting quality
- Gateway overhead under 20 ms

**Scale:** about 50 RPS average, about 300 RPS at peak. A typical request is about 1,500 input and 300 output tokens. Raw prompts must not be logged (PII).

### Functional requirements (Step 4)

1. Accept a chat request and return the LLM's response.
2. Authenticate every request with a team API key, and enforce what that team is allowed to use (models, budget).
3. Route the request to the desired provider or model.
4. Fall back to another provider when one is unavailable or returns errors.
5. Support streaming responses (the support bot needs them).
6. Trace every request: which team sent it, which provider served it, latency, and tokens used.
7. Rate-limit each team so one broken script can't take down everyone else.
8. Cache responses to cut cost and latency.
9. Route cheaper requests to cheaper models when that's good enough, to cut cost.

### Non-functional requirements (Step 4)

| Area | Requirement |
|------|-------------|
| Latency | Gateway adds < 20 ms overhead |
| Scale | Handle 50 RPS average and 300 RPS peak |
| Availability | No client errors when any single provider is down; 99.9% uptime (~43 min of downtime per month) |
| Cost | LLM bill down 30% without hurting quality |
| Privacy | Never store or log raw prompts (they contain customer PII) |

### Estimations (Step 5)

| Quantity | Calculation | Result |
|----------|-------------|--------|
| Requests/day | 50 RPS × ~100k s/day | ~5M |
| Input tokens/day | 5M × 1,500 | 7.5B |
| Output tokens/day | 5M × 300 | 1.5B |
| Input cost/day (assume $3/M) | 7,500 × $3 | $22,500 |
| Output cost/day (assume $15/M) | 1,500 × $15 | $22,500 |
| **Total cost** | | **~$45k/day ≈ $1.35M/month** |
| Value of the 30% target | | **~$400k/month** |
| Concurrent requests at peak (Little's Law) | 300 RPS × 5 s | **1,500 in flight** |
| …when the provider slows to 20 s | 300 RPS × 20 s | **6,000+ in flight** |

**What the numbers tell us:**
- Output tokens are about 1/6 of the volume but half the cost. Cheaper models and `max_tokens` limits are big levers.
- The gateway mostly *waits*. It holds 1,500+ open requests, so it needs async I/O, not a thread per request.
- A **slow** provider is more dangerous than a dead one. Waiting requests pile up until the gateway runs out of connections, file descriptors and memory, and then it fails for everyone, including teams using healthy providers (a cascading failure). We need protection against this (Phase 2).

---

## 0. High-level architecture

**Deployment view.** The gateways are stateless copies, and shared state lives in Redis:

```
            ┌── gateway #1 ──┐
client → LB ┼── gateway #2 ──┼──► Redis (own machine, same data center: ~0.5 ms)
            └── gateway #3 ──┘
                    │
                    ▼
            OpenAI / Anthropic / Ollama
```

**Request path inside one gateway:**

```
Client → [Auth] → [Rate limit] → [Cache] ─hit─► return
                                    │miss
                                    ▼
                                 [Router] → provider ─fail─► fallback provider
                                    │
                                    ▼
                              save to cache → Client

(tracing and metrics on every step; never log raw prompts)
```

- **Auth comes first.** Until then we don't know which team sent the request. Also, serving cache hits before auth would hand cached answers to anyone.
- **The rate limiter comes after auth.** It counts per team, so it needs to know the team.
- **The rate limiter comes before the cache.** A broken script sending the same cached request 50k times per second would still overload the gateway and Redis.
  - *(open: should cache hits count against a team's LLM budget? Maybe two limits in two places?)*
- **The router decides; it doesn't load-balance.** It picks the model and provider, and what to try next on failure. The real load balancer sits in front of the gateway copies.
- **Redis isn't on the gateway machine.** There are 3 copies, so which machine would it live on? And one machine dying would take the cache with it. It must still be in the same data center, because a cross-region call (~80 ms) breaks the 20 ms budget.

**Decision:** stateless gateway copies behind a load balancer, with shared state (cache and rate-limit counters) in Redis.
**Tradeoff:** Redis becomes a dependency on every request, so we must decide what happens when it's down. *(open)*

---

## 1. API contract

- Which OpenAI request fields will you support in v1, and which will you reject or ignore?
- What does an error response look like? (Match OpenAI's error shape?)
- Which custom headers will you add (request ID, cache status, provider)?
- How does a client opt into the semantic cache? (A header? An extra body field?)

**Decision:**
**Tradeoff:**

---

## 2. Provider adapters

- Write out the interface every provider must implement.
- How does an OpenAI-format request map to the Anthropic Messages format? (System prompt, roles, `max_tokens`, stop sequences.)
- How do Anthropic stream events map back to OpenAI chunks?
- How do you normalize `usage` (token counts) across providers?

**Decision:**
**Tradeoff:**

---

## 3. Streaming

- **The relay is a pipe with a window.** Each SSE line is forwarded to the client the moment it arrives. While it passes, the gateway records TTFT (the first content chunk) and `usage` (the last chunk).
- **Usage in streams:** OpenAI only sends `usage` when `stream_options.include_usage=true`, so the gateway always sets it. Without it, cost tracking can't see streamed requests.
- **Fails before the first chunk:** check the upstream status *before* opening the stream. An error becomes a normal error response with the provider's status code. This is the only point where retry or fallback is possible.
- **Fails after the first chunk:** the 200 and part of the answer are already sent, and a restarted answer from another provider would be glued on and garbled. So: send `data: {"error": ...}` (the OpenAI SDK raises `APIError`), no `[DONE]`, and the client decides whether to retry.
- **Client disconnects:** the server cancels the relay. The usage logging lives in `finally:`, so it still runs. Before that fix, 1 in 10 streams lost its usage. If the client leaves before `usage` arrives, we log `usage=None`. *(open: estimate the tokens? decide with budgets)*
- **Unknown fields** (`top_p`, `tools`, …) are kept (`extra="allow"`). Pydantic drops them by default, which would break the drop-in promise.

**Decision:** pass the stream through unchanged; fall back only before the first byte; after that, report errors in the stream.
**Tradeoff:** a mid-stream failure reaches the client as an error rather than being hidden. That's honest and protocol-compatible, but the client has to handle the retry.

---

## 4. Reliability: timeouts, retries, circuit breaker, fallback

**The core split: is it the request's fault or the provider's?** Another provider can't fix a broken request, but it *can* answer when one provider is having trouble.

| Failure | Action | If nothing is left | Why |
|---|---|---|---|
| `400` and other request errors | Return to the client as-is | | Broken everywhere. Falling back would hide the client's bug. |
| `401` / `403` | Fall back, log at **ERROR** | **502** | It's *our* provider key. Passing 401 through would make the client debug their own (valid) key. |
| `429` | Fall back immediately, no retry | **503** + `Retry-After` | The limit is shared by all teams. Retrying in lockstep causes a retry storm; falling back relieves the provider. |
| `5xx` | Fall back immediately | Retry once (100–300 ms jitter), then **502** | Often a brief glitch. Retry only when there's no alternative. |
| Connect error or timeout | Same as 5xx | Retry once, then **502** | The request never reached the provider, so a retry is safe. |
| Read timeout | Fall back, **never retry** | **504** | The provider has the request and may still be working on it (and billing us). Retrying a slow provider piles on load (cascading failure). |
| `x-gateway-fallback: off` | Only the first target is tried | | **Opt-out**, on by default, so every team is protected without reading the docs. Teams that need an exact model (evals) can turn it off. |

**Timeouts:**

| Timeout | Value | Why |
|---|---|---|
| Connect | 3 s | Reaching a server takes milliseconds |
| Stream: first chunk | 10 s | Normally about 1 s, and until the first chunk we can still fall back |
| Stream: gap between chunks | 30 s | A long silence mid-stream means it's stuck (error event; no fallback possible) |
| Non-stream: whole answer | 60 s | Nothing arrives until the end, so we can't tell "slow" from "long". That's the cost of not streaming. |

- **Read ahead before committing.** For a stream, the gateway waits for the provider's first chunk *before* sending `200` to the client. Committing on the provider's headers alone would make fallback impossible if the provider then went silent.
- **Visibility.** `x-gateway-attempts: openai:server, ollama:connect` is on every response that needed fallback, and every attempt is listed in the final error body.
- **Not done yet (deliberately):** fallbacks for specific 400s (context window too small, content policy), like LiteLLM's `context_window_fallbacks`. These are v2.

**Decision:** classify every failure by whose fault it is. Fall back on provider problems, return request errors as they are, and retry only on the last option and only when it's safe.
**Tradeoff:** a fallback can return a different model than the client named. That's reduced by the opt-out and by always reporting the real provider.

- Circuit breaker: what trips it, how long it stays open, how half-open works. Is its state per instance or shared in Redis? *(open: step 2c)*

---

## 5. Auth, rate limiting, budgets

- Where are API keys stored, and how are they looked up?
- Algorithm: token bucket, fixed window, or sliding window? Why?
- How do you make check-and-decrement atomic across N gateway replicas?
- Requests/min and tokens/min: how do you limit tokens when you don't know the output length up front?
- Budget: reserve, then reconcile. Write out the exact steps.
- **If Redis is down:** fail open or fail closed? Decide separately for rate limits, budgets, and the cache.

**Decision:**
**Tradeoff:**

---

## 6. Caching

**Exact cache**

*Flow (cache-aside):*
```
GET key → value found → cache HIT  → return it
        → (nil)       → cache MISS → call LLM → SET key answer → return it
```
- The cache lives in **Redis**, outside the gateway. With several gateway replicas behind a load balancer, every replica sees the same cache. That keeps the gateway servers stateless.
- Whether you save before or after sending doesn't prevent a cache stampede. Identical requests that arrive while the first LLM call is still running all miss either way. (How to solve the stampede is still open.)

*Key design. Anything that changes the answer must be in the key:*

| Part | Why it's in the key |
|------|---------------------|
| tenant | Tenant B must never get Tenant A's answer (isolation) |
| model | A request for the strong model must not get the cheap model's cached answer |
| temperature | Changes how the model writes |
| max_tokens | Can cut the answer short |
| all messages (system + user + assistant) | The system prompt changes the answer a lot ("pirate" vs "lawyer"), not just the last prompt |

- The raw key could be huge (a pasted 300-page document), so **hash it with SHA-256**. The output is a fixed 64 characters, and a one-character change in the input gives a completely different hash.
- Keep fields you need to search or group by **readable, outside the hash**. Example: deleting all of a tenant's data when they leave.

```
cache:<tenant>:<sha256(model + temperature + max_tokens + messages)>
e.g. cache:tenantA:9f3a7c...
```

- What must **never** be cached? *(open: the temperature=1.0 poem question)*
- **TTL** (`SET key value EX <seconds>`): entries delete themselves, which handles both stale answers and the cache filling up.
  - Tradeoff: a short TTL means fresher data but more misses (more cost). A long TTL means more hits but stale answers. Choose it based on how often the data changes.
  - *(open, decide when we build the cache: per-tenant TTL? event-based invalidation? who tells the gateway?)*

**Semantic cache**
- Which embedding model? Local or an API? What latency and cost does it add?
- What is scoped into the lookup (tenant, model, system prompt, params)?
- How will you pick the threshold? Describe the evaluation set and the metric.
- How do you stop wrong answers from being served?

**Decision:**
**Tradeoff:**

---

## 7. Routing / cascade

- What does `model="auto"` mean exactly? Which cheap and strong models?
- What counts as a validation failure? (Give concrete checks.)
- How does a cascade interact with streaming? (Can you validate before streaming?)
- How do you measure whether the cascade is actually worth it?

**Decision:**
**Tradeoff:**

---

## 8. Observability

List every metric: name, type (counter, histogram, gauge), and labels.

| Metric | Type | Labels | Why |
|--------|------|--------|-----|
|        |      |        |     |

- Which labels would cause a cardinality explosion?
- What would you alert on, and at what thresholds?
- What goes in the logs? What must never go in them?

**Decision:**
**Tradeoff:**

---

## 9. Load testing

- What exactly are you measuring? (Gateway overhead vs end-to-end.)
- How does the mock provider behave (latency distribution, error rate, streaming)?
- What test scenarios will you run (steady load, spike, provider outage, cache-heavy)?
- What's the bottleneck likely to be, and how will you confirm it?

**Results:**

| Scenario | RPS | p50 | p95 | p99 | Error % |
|----------|-----|-----|-----|-----|---------|
|          |     |     |     |     |         |

---

## 10. Scaling and failure scenarios (interview drill)

Answer each one in 3–5 sentences:

1. Traffic goes up 20×. What breaks first?
2. Redis goes down.
3. OpenAI is degraded: slow, but not erroring.
4. One tenant sends a burst of 10k requests.
5. A provider fails halfway through a stream.
6. You need to add a new provider. What changes in the code?

---

## 11. Decision log

All decisions, with alternatives, pros and cons, production terms, and reading: see **[DECISIONS.md](DECISIONS.md)**.
