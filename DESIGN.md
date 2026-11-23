# switchboard: system design

The problem, the requirements, the numbers behind them, and the shape of the system. Each section ends with the decision and what it costs. The full reasoning for every choice, with the alternatives, is in [DECISIONS.md](DECISIONS.md).

---

## Problem

About 20 internal teams call LLM providers directly from their backend services. Today:
- a provider outage took the support bot down for 2 hours;
- nobody can tell which team is driving a bill that grew 4×;
- one team's runaway script used up the shared rate limit and broke every other team.

switchboard takes a chat request plus a team API key, routes it to one of several providers (OpenAI, Anthropic, Groq, local Ollama), and returns the response.

**Success means:**
- no client errors when any single provider is down
- every request attributed to a team
- LLM cost down 30% without hurting quality
- gateway overhead under 20 ms

**Scale:** about 50 requests/s on average and 300 at peak. A typical request is about 1,500 input and 300 output tokens. Raw prompts must never be logged (they contain customer PII).

### Functional requirements

1. Accept a chat request and return the LLM's response, in OpenAI's format.
2. Authenticate every request with a team API key, and enforce what that team may use (models, budget).
3. Route the request to the right provider and model.
4. Fall back to another provider when one is unavailable or failing.
5. Stream responses.
6. Record every request: team, provider, latency, tokens, cost.
7. Rate-limit each team so one broken script can't take down everyone else.
8. Cache responses to cut cost and latency.
9. Send easy requests to cheaper models.

### Non-functional requirements

| Area | Requirement |
|------|-------------|
| Latency | Gateway adds < 20 ms |
| Scale | 50 requests/s average, 300 peak |
| Availability | No client errors when any single provider is down; 99.9% uptime (~43 min of downtime a month) |
| Cost | LLM bill down 30% without hurting quality |
| Privacy | Never store or log raw prompts |

### Estimates

| Quantity | Calculation | Result |
|----------|-------------|--------|
| Requests/day | 50/s × ~100k s/day | ~5M |
| Input tokens/day | 5M × 1,500 | 7.5B |
| Output tokens/day | 5M × 300 | 1.5B |
| Input cost/day (at $3/M) | 7,500 × $3 | $22,500 |
| Output cost/day (at $15/M) | 1,500 × $15 | $22,500 |
| **Total cost** | | **~$45k/day ≈ $1.35M/month** |
| Value of the 30% target | | **~$400k/month** |
| Requests in flight at peak (Little's Law) | 300/s × 5 s | **1,500** |
| ...when the provider slows to 20 s | 300/s × 20 s | **6,000+** |

What the numbers say:
- Output tokens are a sixth of the volume but half the cost. Cheaper models and `max_tokens` limits are the big levers.
- The gateway mostly waits. It holds 1,500+ open requests, so it needs async I/O, not a thread per request.
- A slow provider is more dangerous than a dead one. Waiting requests pile up until the gateway runs out of connections, and then it fails for everyone, including teams on healthy providers. The chaos test confirmed this (section 9).

---

## 1. Architecture

The gateway copies are stateless. Shared state lives in Redis and Postgres:

```
                 ┌── gateway #1 ──┐      Redis        rate-limit buckets, spend totals
client → nginx ──┼── gateway #2 ──┼───►  Redis (2nd)  cached answers, evicts when full
                 └── gateway #3 ──┘      Postgres     keys, usage ledger, audit log
                         │
                         ▼
             OpenAI / Anthropic / Groq / Ollama
```

Inside one copy, every request goes through:

```
auth → rate limit → budget → cache ─hit─► return
                               │ miss
                               ▼
                            router → provider ─fail─► fallback provider
                               │
                               ▼
                       record usage, save to cache → client
```

- **Auth first.** Until then we don't know the team, and serving cache hits before auth would hand cached answers to anyone.
- **Rate limit before the cache.** A broken script sending the same cached request 50k times a second would still overload the gateway and Redis.
- **The router decides; it doesn't load-balance.** It picks the model and provider, and what to try next. nginx balances across the copies.
- **Redis is separate from the gateway machines** but in the same data center: a cross-region call (~80 ms) would break the 20 ms budget on its own.

**Decision:** stateless copies behind a load balancer, with shared state in Redis and Postgres.
**Cost:** Redis is on every request's path, so each use of it needs a rule for when it's down (section 5).

---

## 2. API contract

- `POST /v1/chat/completions`, streamed or not, in OpenAI's format. Fields the gateway doesn't model are passed through to the provider, so the OpenAI SDK works unchanged.
- Errors use OpenAI's shape (`{"error": {"message", "type", "code"}}`), so SDK retry logic works too.
- Response headers say what happened: `x-request-id`, `x-gateway-provider`, `x-gateway-cache`, `x-gateway-attempts`, rate-limit and budget headers.
- Clients opt into extras with headers: `x-gateway-cache`, `x-gateway-fallback`, `x-gateway-tags`.

**Decision:** be a drop-in replacement for the OpenAI API (DECISIONS.md C, U).
**Cost:** every message shape OpenAI accepts has to be accepted here too, including images and tool calls.

---

## 3. Providers

Anything that speaks OpenAI's API (OpenAI, Groq, Ollama, vLLM) needs only a config entry. Anthropic is translated by an adapter: system prompt, roles, `max_tokens`, stop sequences on the way out; stream events and usage on the way back. Routing, fallback and circuit breaking only ever see OpenAI's format.

**Decision:** one canonical format, with provider differences kept in adapters at the bottom of the call path (DECISIONS.md I).
**Cost:** features one provider lacks (Claude has no `tools` here) mean skipping that provider for those requests.

---

## 4. Streaming

- Each SSE chunk goes to the client as soon as it arrives. On the way through, the gateway records TTFT (first content chunk) and usage (last chunk). It always asks OpenAI for usage, and removes it again if the client didn't ask.
- **Commit point:** the gateway waits for the provider's first chunk before sending `200`. Until then it can still fall back silently.
- **After the first chunk**, the answer can't be restarted elsewhere without garbling it. A failure becomes an error event and the client decides whether to retry.
- **Client disconnects:** usage logging runs in `finally:`, so it still happens.

**Decision:** pass the stream through, fall back only before the first chunk, report later errors in the stream (DECISIONS.md D).
**Cost:** a mid-stream failure reaches the client as an error rather than being hidden.

---

## 5. Reliability

**The core split: is it the request's fault or the provider's?** Another provider can't fix a broken request, but it can answer when one provider is in trouble.

| Failure | Action | If nothing is left | Why |
|---|---|---|---|
| `400` and other request errors | Return as is | | Broken everywhere. Falling back would hide the client's bug. |
| `401` / `403` | Fall back, log at ERROR | **502** | It's our provider key. Passing 401 through would make the client debug their own valid key. |
| `429` | Fall back, no retry | **503** + `Retry-After` | Retrying in lockstep causes a retry storm. |
| `5xx` | Fall back | Retry once with jitter, then **502** | Often a brief glitch. Retry only when there's no alternative. |
| Connect error | Same as 5xx | Retry once, then **502** | The request never arrived, so a retry is safe. |
| Read timeout | Fall back, **never retry** | **504** | The provider may still be working on it, and billing us. Retrying piles on load. |

| Timeout | Value | Why |
|---|---|---|
| Connect | 3 s | Reaching a server takes milliseconds |
| Stream: first chunk | 10 s | Normally ~1 s, and until then we can still fall back |
| Stream: gap between chunks | 30 s | A long silence mid-stream means it's stuck |
| Non-stream: whole answer | 60 s | Nothing arrives until the end, so slow and long look the same |

- **Circuit breaker** per provider-and-model: 5 failures in a row open it, and the model is skipped. After 30 s, one trial request goes through. State is per copy, not shared: 3 copies learning separately waste 15 requests instead of 5, which isn't worth a Redis round trip on every request (DECISIONS.md G).
- **Bulkhead:** each provider may use at most 600 of the 1,000 connections. A slow call (over 10 s) counts as a breaker failure even if it succeeds (DECISIONS.md R).
- **Load shedding:** when a copy's own work starts taking over 100 ms, it refuses new requests with a fast 503 instead of queueing them (DECISIONS.md Q).
- **If Redis is down:** rate limits are enforced per copy, budgets aren't checked (spend is still recorded), and the cache is a miss. **If Postgres is down:** keys already cached keep working; new ones are rejected. Fail closed on security, fail open on protection.

**Decision:** classify every failure by whose fault it is; fall back on the provider's, return the client's, retry only when safe and nothing else is left.
**Cost:** a fallback can answer with a different model than the client named. Clients can turn fallback off, and the real provider is always reported.

---

## 6. Keys, rate limits, budgets

- **Keys:** Postgres stores a SHA-256 hash, never the key. Each copy caches lookups for 60 s. A key can carry its own limits, budget and expiry inside its team's, and is rotated with a grace period (DECISIONS.md J, T).
- **Rate limits:** token buckets in Redis, requests per minute and tokens per minute, per team and per key. All the buckets are checked and charged in one Lua script, so two copies can't spend the same token. Tokens are estimated before the call and settled from the provider's usage report after (DECISIONS.md K).
- **Budgets:** every request is priced by the model that answered and written to an append-only ledger in Postgres, in whole micro-dollars. Redis keeps a running total per team and key, rebuilt from the ledger if lost (DECISIONS.md L).
- **The gateway's database user** can read keys, insert usage rows, and update one column. It can't create keys or change spend (DECISIONS.md M).

**Decision:** Redis for the per-request checks, Postgres as the source of truth.
**Cost:** a budget can overshoot by the requests already in flight when it runs out.

---

## 7. Caching

**Exact cache.** Cache-aside in its own Redis, which evicts the least-used answers when full, so cached answers never push out rate-limit data. The key is `cache:<team>:<sha256(model, parameters, all messages)>`: anything that changes the answer is in the key, and teams never share entries. Off unless the team asks, because a cached "write a poem" never gives a new poem. Only complete first-choice answers are stored (DECISIONS.md N).

**Semantic cache.** For a new question, embed it with `text-embedding-3-small`, find the closest stored question with the same model, system prompt and parameters, and ask gpt-4o-mini whether the two mean the same thing. A similarity threshold alone isn't safe: real paraphrases scored 0.58–0.87, while near-misses like "refund *after* 30 days" vs "*within* 30 days" scored up to 0.96. Shadow mode runs the lookup without serving it, so a team can measure it first. Any slow or failed step is a miss (DECISIONS.md O).

**Decision:** exact first, then embedding search to find a candidate and an LLM judge to decide.
**Cost:** the judge adds ~0.7 s to candidates, and still makes mistakes: 4.2% of hits in the replay (DECISIONS.md S).

---

## 8. Routing

`model: "auto"` sends a request to gpt-4o-mini, and upgrades to gpt-4o if it offers tools, has 6+ user messages, or has 3,000+ prompt tokens. A router decides before calling anything; a cascade (cheap model, judge, escalate) would add a judge call to every request and hold back the stream. Shadow mode also sends 5% of `auto` requests to the other model, and an offline judge compares the two answers, which gives the data to tune the rules later (DECISIONS.md P).

**Decision:** rules now, a trained classifier once there's labelled data.
**Cost:** rules only guess from the request; shadow mode measures how often they guess wrong.

---

## 9. Measured results

Load tests run open-loop with k6 against a mock provider with a fixed delay, so any added latency is the gateway's. One copy on one CPU, with py-spy for profiles (DECISIONS.md Q):

| Scenario | Requests/s | Shed | p50 | p99 |
|----------|-----|------|-----|-----|
| Non-streamed | 300 | 0 | +2 ms | +97 ms |
| Non-streamed | 500 | 5% | +5 ms | +308 ms |
| Streamed, time to first token | 200 | 5% | +9 ms | +245 ms |
| Exact cache hits, total | 800 | 3% | 1.0 ms | 160 ms |

**Bottlenecks found:** httpx's default pool of 100 connections (~90 requests/s), then httpcore's pool bookkeeping, which grows with the square of open connections (88% of the CPU at 150 requests/s). Provider calls now go through aiohttp: about 3× the throughput.

**Provider outage** (100 requests/s, first-choice provider broken for 60 s, DECISIONS.md R): killed or returning 500s, 100% of requests succeeded through the fallback. Slow (20 s per answer), only 60% did at first: slow answers counted as successes and took the whole connection pool. With the bulkhead and slow-call breaker, 100%.

**Cache replay** (1,000 support questions, DECISIONS.md S): 57% semantic hits, 38% lower cost after paying for the embedding and judge calls.

---

## 10. Failure scenarios

- **Traffic goes up 20×.** Add copies; they're stateless. Redis is next: one Lua script per request is cheap, but it's a single instance. Past that, shard buckets by team.
- **Redis goes down.** Rate limits fall back to each copy's share, budget checks pause, the cache misses. Requests keep flowing.
- **A provider is slow but not failing.** The bulkhead caps its connections, and the slow-call breaker moves traffic to the fallback after five slow answers.
- **One team sends 10k requests at once.** Its bucket empties and it gets 429s with `Retry-After`; other teams are untouched.
- **A provider fails mid-stream.** The client gets an error event, not a truncated answer that looks complete.
- **A new provider.** If it speaks OpenAI's API, a config entry. Otherwise, one adapter.
