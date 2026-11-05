# Decisions

Every design decision in this project: what we chose, what we rejected, why, and what it costs.
Each entry also gives the **production term** for the idea (the words to use in interviews) and, where it really helps, something to read.

**How to read an entry:** *I chose X over Y because Z. The cost is W, and we reduce it by V.* That one sentence is the interview answer.

Decided 2026-11-02 → 2026-11-03. The newest decisions are at the bottom of each section.

---

## A. Architecture (from the system design)

### A1. Stateless gateway copies; shared state in Redis
- **Decision:** run several identical gateway copies behind a load balancer. Nothing shared lives in a copy's memory. The cache and rate-limit counters live in Redis.
- **Alternatives:** keep state inside each copy (for example, a Python `dict` as the cache).
- **Why:** with in-memory state, a request that lands on copy #3 can't see what copy #1 cached, and a restart or deploy wipes it.
- **Pros:** add copies to handle more traffic; any copy can serve any request; deploys don't lose data.
- **Cons:** every request now depends on a network call to Redis. *(Open: what happens when Redis is down? See A6.)*
- **Production terms:** *stateless service*, *horizontal scaling*, *externalized state*.
- **Read:** Kleppmann, *Designing Data-Intensive Applications*, ch. 1.

### A2. Redis on its own machine, in the same data center
- **Decision:** Redis is a separate server, not on a gateway machine, but in the same data center or region.
- **Alternatives:** Redis on one of the gateway machines; Redis in another region.
- **Why:** with 3 gateway copies, putting Redis on one of them makes that machine special, and its failure takes the cache down too. In another region, each call is about 80 ms, which alone breaks our 20 ms overhead budget. In the same data center it's about 0.5 ms.
- **Production terms:** *network latency budget*, *single point of failure (SPOF)*, *managed service* (for example, AWS ElastiCache).
- **Read:** "Latency Numbers Every Programmer Should Know" (a one-page chart).

### A3. Pipeline order: auth → rate limit → cache → router → provider
- **Decision:** authenticate first, then rate-limit, then check the cache, and only then route to a provider.
- **Alternatives:** cache before auth (skip auth on cache hits).
- **Why:** a cache before auth would hand cached answers to anyone, even without a key. The rate limiter needs to know the team, so it has to come after auth.
- **Production terms:** *request pipeline*, *middleware chain*, *authentication (who are you) vs authorization (what are you allowed to do)*.

### A4. Rate limiter before the cache
- **Decision:** rate limits apply even to requests that would be cache hits.
- **Alternatives:** rate-limit only after a cache miss, because a cache hit costs almost nothing.
- **Why:** a broken script sending the same cached request 50,000 times a second still overloads the gateway and Redis, even though the LLM is never called.
- **Cons:** a team's cache hits use up its request limit. *(Open: two limits? A cheap flood limit early, and a token/budget limit just before the LLM call.)*
- **Production terms:** *noisy neighbor* (one tenant hurting the others; our "broken script" problem), *load shedding*, *multi-tenancy*.
- **Read:** Stripe Engineering blog, "Scaling your API with rate limiters."

### A5. Async I/O
- **Decision:** async Python (FastAPI with `async`/`await`), not one thread per request.
- **Why:** from Little's Law, 300 RPS × 5 s per LLM call means **about 1,500 requests in flight** at peak, and 6,000+ if a provider slows to 20 s. The gateway mostly *waits*. Async lets one process wait on thousands of requests cheaply.
- **Production terms:** *Little's Law* (L = λ × W), *I/O-bound vs CPU-bound*, *event loop*, *concurrency vs parallelism*.
- **Read:** Wikipedia, "Little's law" (just the intro and the examples).

### A6. *(Open)* What happens when Redis is down?
- To decide in Phase 3: **fail open** (skip the cache or limit and keep serving) or **fail closed** (reject requests), decided separately for the cache, rate limits, and budgets.

---

## B. Cache (designed; built in Phase 4)

### B1. Cache-aside
- **Decision:** on a request, `GET` the key. On a hit, return it. On a miss (`nil`), call the LLM, `SET` the answer, then return it.
- **Alternatives:** write-through (the cache sits in front of the source and writes through it). That doesn't fit, because an LLM isn't a database we write to.
- **Note:** the order of saving and sending doesn't prevent a *cache stampede*. Identical requests that arrive during the first LLM call all miss either way. *(Open: request coalescing.)*
- **Production terms:** *cache-aside / lazy loading*, *cache hit / miss*, *cache stampede / thundering herd*, *request coalescing / single-flight*.
- **Read:** AWS Builders' Library, "Caching challenges and strategies."

### B2. Cache key: `cache:<tenant>:<sha256(model + temperature + max_tokens + messages)>`
- **Decision:** everything that changes the answer goes into the key. It's hashed for a fixed size, with the tenant kept readable as a prefix.
- **Why each part:**
  - **tenant:** B must never get A's answer.
  - **model:** a strong-model request must not get the cheap model's answer.
  - **temperature, max_tokens:** both change the output.
  - **all messages:** the system prompt changes the answer ("pirate" vs "lawyer").
  - **hash:** a 300-page prompt would otherwise be the key, and SHA-256 is always 64 characters.
  - **readable tenant prefix:** lets us find or delete all of one tenant's keys.
- **Production terms:** *tenant isolation*, *cache key normalization*, *key namespacing*, *cryptographic hash*.

### B3. Entries expire with a TTL
- **Decision:** every entry gets a TTL (`SET key value EX <seconds>`).
- **Why:** it handles both stale answers (the refund policy changed) and a cache that fills up forever.
- **Tradeoff:** a short TTL means fresher answers but more misses, which costs money. A long TTL means more hits but stale answers.
- *(Open: a per-tenant TTL? Event-based invalidation? Who tells the gateway that data changed?)*
- **Production terms:** *TTL*, *cache invalidation* (time-based vs event-based), *staleness*, *eviction policy (LRU)*.

---

## C. Foundation (Phase 0)

### C1. Python + FastAPI + httpx + Pydantic
- **Why:** the standard production stack for async Python APIs. Pydantic validates requests, and httpx is an async HTTP client with connection pooling.
- **Alternatives:** Go (faster, but less familiar, and the AI/ML ecosystem is Python); Flask (not async-first).

### C2. One shared HTTP client for the whole app
- **Decision:** create one `httpx.AsyncClient` at startup (`lifespan`) and reuse it for every request.
- **Alternatives:** create a new client per request.
- **Why:** a new client per request opens a new TCP and TLS connection to the provider every time, which is slow and exhausts sockets. A shared client keeps a pool of connections open.
- **Production terms:** *connection pooling*, *keep-alive*, *TLS handshake cost*, *file descriptor limits*.
- **Read:** httpx docs, "Resource Limits" and "Timeouts."

### C3. Secrets from the environment (`.env` in development); fail fast at startup
- **Decision:** API keys come from environment variables, never from code. The config file holds only the *name* of the env var. A missing key stops startup with a clear error.
- **Why:** code ends up on GitHub. Failing at startup means a broken deploy is caught at deploy time, not on the first request at 3 a.m.
- **Production terms:** *12-factor config*, *secrets management*, *fail fast*.
- **Read:** 12factor.net, "III. Config."

### C4. Validate requests before calling the provider
- **Decision:** Pydantic checks the request shape. A missing `model` gets a 422 immediately, and the provider is never called.
- **Why:** it's free and takes about 1 ms instead of a paid round trip. And a 4xx tells the client to fix their request, not to retry.
- **Production terms:** *input validation at the edge*, *fail fast*.

### C5. Forward fields we don't model
- **Decision:** `extra="allow"`, so `top_p`, `tools`, `response_format`, and other fields reach the provider.
- **Why:** Pydantic silently **drops** unknown fields by default, which would quietly break the "drop-in replacement" promise.
- **Production terms:** *API compatibility*, *drop-in replacement*, *forward compatibility*.

---

## D. Streaming (Phase 1)

### D1. Pass the SSE stream through unchanged
- **Decision:** forward every line the moment it arrives. The gateway only watches (TTFT and usage) and changes nothing.
- **Why:** users see the first words in about 1 s instead of waiting for the whole answer.
- **Production terms:** *Server-Sent Events (SSE)*, *time to first token (TTFT)*, *streaming proxy*.
- **Read:** MDN, "Using server-sent events."

### D2. The gateway always asks for usage in streams
- **Decision:** set `stream_options.include_usage = true` on every streamed request.
- **Why:** OpenAI only sends token usage at the end of a stream if asked. Without it, cost tracking, budgets, and token limits can't see streamed requests.
- **Production terms:** *usage metering*, *cost attribution*.

### D3. Usage logging always runs (`finally`)
- **Decision:** the code that records usage runs in `finally:`, so it runs even when the client disconnects.
- **Why:** we measured that **1 in 10 streams lost its usage** when the client closed the connection. That's lost cost data.
- **Cons:** if the client leaves *before* the provider sends `usage`, we still don't know the tokens, so we log `usage=None`. *(Open: estimate them; decide with budgets.)*

### D4. Mid-stream failure: send an error event, don't regenerate
- **Decision:** if the provider dies after chunks were sent, send `data: {"error": ...}` without `[DONE]`. The OpenAI SDK raises `APIError`.
- **Alternatives:** regenerate with another provider and continue the stream.
- **Why:** the 200 and part of the answer are already sent, and bytes can't be unsent. A regenerated answer gets **glued on** ("…within 30 Thanks for asking!…") because the OpenAI protocol has no "discard what you got" message. The gateway must not invent behavior the protocol doesn't have. The client knows its own context (show a "Regenerate" button, or retry a job).
- **Production terms:** *response commit point*, *protocol compatibility*, *partial failure*.

### D5. Read ahead: commit only after the first chunk
- **Decision:** for streams, wait for the provider's **first chunk** before sending 200 to the client.
- **Alternatives:** commit as soon as the provider sends its headers.
- **Why:** fallback is only possible before the first byte reaches the client. A provider that sends headers and then goes silent would otherwise leave us stuck.
- **Production terms:** *commit point*, *buffering / read-ahead*.

---

## E. Routing (Phase 2a)

### E1. Fallback chains live in a central config owned by the platform team
- **Decision:** `config.yaml` maps each model name a client can request to an ordered list of `{provider, model}` targets.
- **Alternatives:**
  - per-request fallback lists, where the client sends the list (OpenRouter's `models` parameter);
  - a config sent with each request (Portkey's configs).
- **Why:** 20 internal teams and leadership's cost goals call for central governance, and client code stays one line.
- **Cons:** a team that needs a custom chain has to ask the platform team. A per-request override could be added later.
- **Production terms:** *model routing*, *routing table*, *governance*, *control plane vs data plane*.
- **Read:** the LiteLLM docs on proxy config (`model_list`) and on router fallbacks.

### E2. Clients may send a vendor name **or** a tier name
- **Decision:** both `gpt-4o-mini` and `fast` / `strong` are valid; the config decides the real model.
- **Why:** vendor names keep existing code working (teams change only `base_url`). Tiers let teams say what they need without picking a vendor, and let us swap vendors later.
- **Cons:** during a fallback, a request for `gpt-4o-mini` may be answered by Llama. **Reduced by** always reporting the real provider (`x-gateway-provider`) and model (`model` in the response).
- **Production terms:** *model aliasing*, *model groups*, *vendor lock-in*.

### E3. An unknown model gets a 404 `model_not_found`
- **Decision:** a model name that isn't in the config is rejected without calling any provider.
- **Why:** the platform controls which models teams can use. It's also the same error OpenAI returns, so client code handles it.
- **Production terms:** *allowlist*, *authorization*.

---

## F. Failure handling (Phase 2b)

**The core rule: is it the request's fault or the provider's?** Another provider can't fix a broken request, but it can answer when one provider is in trouble.

### F1. Request errors (400, etc.) are returned as-is: no retry, no fallback
- **Alternatives:** retry (the same input gives the same error); fall back (a lenient provider might accept it, but that **hides the client's bug**).
- **Later (v2):** some 400s are *model limits*, not client mistakes: context window too small, or a content-policy block. LiteLLM has `context_window_fallbacks` and `content_policy_fallbacks` for these.
- **Production terms:** *4xx = client error, 5xx = server error*, *fail fast*.

### F2. Fallback is on by default; clients can opt out
- **Decision:** `x-gateway-fallback: off` disables it for one request.
- **Alternatives:** opt-in (off by default; the user's original idea); always on, with no flag.
- **Why:** **the default decides what happens to everyone who doesn't read the docs.** With opt-in, the support bot is down again in the next outage. Teams that need an exact model (evals) can opt out.
- **Industry:** OpenRouter falls back by default (`allow_fallbacks: false` turns it off). LiteLLM applies configured fallbacks automatically.
- **Production terms:** *failover*, *graceful degradation*, *secure / safe defaults*, *opt-in vs opt-out*.

### F3. An upstream 401/403 is *our* problem: fall back, log at ERROR, and return 502 if nothing is left
- **Alternatives:** pass the 401 through (the client then debugs their own valid key for hours).
- **Why:** teams never send provider keys. The gateway uses **one shared OpenAI key**, so a 401 means *our* key is broken, for every team.
- **Note:** in a **BYOK** design (each user sends their own provider key), a 401 would be that user's fault and should be passed through.
- **Production terms:** *502 Bad Gateway* ("something behind the gateway failed"), *BYOK (bring your own key)*.

### F4. 429: fall back immediately; return 503 with `Retry-After` if nothing is left
- **Alternatives:** wait and retry the same provider (LiteLLM does this, because it often has several deployments of one model).
- **Why:** the 429 is for our **shared** account. Retrying in lockstep across all teams hammers the provider again, and falling back takes load off it.
- **Production terms:** *retry storm*, *backpressure*, *`Retry-After` header*, *503 Service Unavailable*.

### F5. 5xx and connect errors: fall back immediately; on the last option, retry **once** with jitter, then 502
- **Alternatives:** always retry first (slow during real outages); never retry (gives up on one-second glitches when there's no alternative).
- **Why:** fall back fast when an alternative exists. Retry only when nothing else is left, after a random 100–300 ms wait so failed requests don't all retry at the same instant.
- **Production terms:** *exponential backoff with jitter*, *retry budget*, *transient failure*.
- **Read:** AWS Builders' Library, "Timeouts, retries, and backoff with jitter"; Marc Brooker, "Exponential Backoff and Jitter" (AWS Architecture Blog).

### F6. Read timeouts are never retried; 504 if nothing is left
- **Why:** the provider *received* the request and may still be working on it, and billing us. Retrying a **slow** provider piles more requests onto it (a cascading failure). A connect timeout is different: the request was never sent, so it's safe to retry.
- **Production terms:** *idempotency* (is it safe to do twice?), *cascading failure*, *504 Gateway Timeout*.
- **Read:** Google SRE Book, "Addressing Cascading Failures" (free online).

### F7. Timeout values
| Timeout | Value | Why |
|---|---|---|
| Connect | 3 s | Reaching a server takes milliseconds |
| Stream: first chunk | 10 s | Normally about 1 s, and until then we can still fall back |
| Stream: gap between chunks | 30 s | A long silence means it's stuck |
| Non-stream: whole answer | 60 s | Nothing arrives until the end, so slow and long look the same. That's the cost of not streaming. |
- **Production terms:** *connect / read / total timeouts*, *time to first byte (TTFB)*, *deadline*.

---

## G. Circuit breaker (Phase 2c)

The problem: during a full outage, every request still tries the dead provider first and waits up to 3–60 s before falling back. At 300 RPS that means thousands of requests waiting on a provider we already know is dead.

### G1. What counts as a failure: anything that triggers a fallback
- **Decision:** 5xx, connect errors, timeouts, **401, and 429** all count. Request errors (400) never count.
- **Alternatives:** count only "really down" errors (5xx, connect, timeout).
- **Why:** what matters is whether **we** can use the provider, not whether its servers are technically up. And one buggy team sending bad requests must never cut a provider off for all 20 teams.
- **Industry:** LiteLLM puts a deployment into *cooldown* after repeated failures, including 429s.

### G2. Trip after 5 consecutive failures
- **Alternatives:** a failure rate in a time window (for example, ≥50% of the last 30 s, with at least 20 requests).
- **Why:** it's simple (one counter) and detects a full outage in about 17 ms at 300 RPS. A partial outage won't trip it, but each failed request already falls back (F2–F6), so users still get answers.
- **Industry:** Envoy's outlier detection defaults to 5 consecutive errors. resilience4j uses failure-rate windows.
- **v2:** move to a failure-rate window if partial outages start hurting latency.

### G3. Recovery: a 30 s cooldown, then one trial request; everyone else keeps falling back
- **Alternatives:** background health checks (extra moving parts, and a ping can succeed while chat completions are broken).
- **Why:** it tests the real thing, and if the trial fails, that user still gets an answer through fallback. **Only one trial at a time**, because 300 simultaneous trials would knock a recovering provider straight back down.
- **The trial can't get stuck:** a hung trial ends at the 2b timeouts and counts as a failure.
- **v2:** a growing cooldown (30 s → 60 s → 120 s) for a provider that keeps failing its trials.
- **Production terms:** *circuit breaker states: closed / open / half-open*, *cooldown*, *probe / trial request*.

### G4. Breaker state lives in each gateway copy's memory
- **Alternatives:** share the state in Redis.
- **Why:** with 3 copies, each one learns separately, so 15 wasted requests instead of 5. That's about 0.05 s of traffic at 300 RPS, and each of those requests still falls back. Sharing the state would add a Redis call to every request **and** make Redis a new way for the breaker itself to fail.
- **Industry:** Envoy's outlier detection is per proxy instance. resilience4j and Polly keep state in memory by default.
- **Interview line:** *"I deliberately didn't share breaker state, because the coordination cost more than the inconsistency."*
- **Read:** Martin Fowler, "CircuitBreaker" (martinfowler.com/bliki/CircuitBreaker.html); Michael Nygard, *Release It!*, the book that popularized the pattern.

### G5. One breaker per provider + model, not per provider
- **Alternatives:** one breaker per provider.
- **Why:** providers often have trouble with one model at a time. Per provider, a broken `gpt-4o` would also cut off a healthy `gpt-4o-mini`.
- **Cons:** a full provider outage needs 5 failures *per model* before each breaker trips. That's a few more wasted requests, and each still falls back.
- **Industry:** LiteLLM cools down per *deployment* (one model at one provider).
- **Production terms:** *failure domain*, *blast radius*, *deployment*.

### G6. Implementation details that came out of building it
- **When skipped, return `503` with `Retry-After`** set to the seconds left until the next trial, so clients know when to come back. We skipped the provider ourselves, so this is "temporarily unavailable" (503), not "bad gateway" (502).
- **Any real answer counts as success, even a 400.** It proves the provider is up and responding.
- **Late results are ignored.** A slow request that started before the trip and finishes afterward can't close the breaker. Only the trial decides.
- **An abandoned trial frees its slot.** If the trial request is cancelled (the client disconnects), the slot is released. Otherwise the breaker would be stuck half-open forever.
- **No locks:** one asyncio event loop, and the breaker methods never `await`, so requests can't interleave inside them.
- *(Open)* **A provider dying mid-stream doesn't count as a breaker failure** (the stream already succeeded at its first chunk). Count it later if it turns out to happen often.
- **Measured:** with a broken OpenAI key and fallback off, requests 1–5 took **0.3–0.75 s** each to fail at OpenAI. Requests 6–7 failed in **~1.3 ms**, because the breaker skipped OpenAI entirely.

## H. Metrics (Phase 2d)

### H1. Two dashboards: on-call health vs cost/usage
- **Decision:**
  - **On-call dashboard, top row:** traffic (RPS), error rate (5xx as the client sees it), latency (p95 overall, and TTFT for streams). This is the **RED method**.
  - **On-call dashboard, second row (gateway-specific):** fallback rate, breaker state per deployment, requests in flight (saturation).
  - **Cost/usage dashboard:** tokens and $ per model, and per team once Phase 3 adds team keys.
- **Alternatives:** one dashboard with everything; or only latency, cost, and errors (the first pick).
- **Why:**
  - Cost matters a lot (the 30% goal is worth about $400k a month), but it changes slowly. It's read daily or weekly by the platform team and finance, not at 3 a.m. It's a **business metric**, not an **operational** one.
  - **Traffic is required:** if no requests reach the gateway, errors read 0% and latency looks fine. Only RPS dropping to 0 shows it. It's also what makes an error count meaningful.
  - **Gateway-specific metrics catch hidden degradation:** when OpenAI is down but fallback covers it, the client error rate stays at 0%. Only the fallback-rate and breaker graphs show that every team is running on the backup model.
- **Cons:** two dashboards to maintain.
- **Production terms:** *RED method* (Rate, Errors, Duration), *Google's four golden signals* (latency, traffic, errors, saturation), *operational vs business metrics*, *SLI* (the measured number, e.g. % of non-5xx requests), *SLO* (the target, e.g. 99.9%), *degraded mode*.
- **Read (only if needed):** Google SRE book, chapter 6 "Monitoring Distributed Systems" (sre.google/sre-book/monitoring-distributed-systems/); Tom Wilkie, "The RED Method" (Grafana blog).

### H2. Tool: Prometheus + Grafana
- **Decision:** the gateway exposes `GET /metrics`; Prometheus scrapes it every 15 s; Grafana draws the dashboards. Both run locally with Docker Compose.
- **Alternatives:** OpenTelemetry (push to a collector; also does tracing); Datadog / New Relic (paid SaaS agents).
- **Why:** the open-source standard (LiteLLM, Envoy, and Kubernetes all expose `/metrics`), free, and anyone who clones the repo can run the same dashboard.
- **Cons:** two more containers. Docker is needed now instead of in Phase 3 (Redis needs it anyway).
- **Later:** add OpenTelemetry if we want **traces** that follow one request through every step.
- **Production terms:** *pull model / scraping*, *scrape interval*, *exposition format*, *time-series database (TSDB)*, *observability: metrics, logs, traces (the three pillars)*.

### H3. Which metrics, their types, and their labels
| Metric | Type | Labels | Answers |
|---|---|---|---|
| `gateway_requests_total` | counter | model, status | traffic (RPS) and error rate |
| `gateway_request_duration_seconds` | histogram | model | p95 latency, **including the whole stream** |
| `gateway_requests_in_flight` | gauge | none | saturation (Little's Law, A-section estimates) |
| `gateway_upstream_attempts_total` | counter | deployment, outcome | which provider is failing and how (`ok`, `server`, `circuit_open`, `mid_stream`...) |
| `gateway_fallbacks_total` | counter | model | degraded mode that clients can't see |
| `gateway_breaker_state` | gauge, one-hot | deployment, state | which breakers are open right now |
| `gateway_ttft_seconds` | histogram | deployment | streaming responsiveness |
| `gateway_tokens_total` | counter | deployment, kind | usage, and cost once we add prices |
- **Types:** counter = only goes up (Prometheus computes the rate); gauge = goes up and down; histogram = buckets, for percentiles.
- **Breaker state is one-hot** (one series per state, set to 1 or 0) instead of label-encoded 0/1/2, so "how many breakers are open" is a plain `sum`. This is Prometheus's *state set* pattern.
- **Labels only with a small, fixed set of values.** Never `prompt` or `request_id`: each unique value is a new time series kept in memory (*cardinality explosion*), and `prompt` would also break the no-raw-prompts rule. Model names the client sends that aren't configured are labelled `unknown`, so a client can't create series. `team` comes in Phase 3 (about 20 values: fine).
- **Two model labels:** `model` is what the client asked for (`fast`); `deployment` is what we actually called (`openai/gpt-4o-mini`), the same name as the breakers.
- **Buckets go up to 60 s.** The library defaults stop at 10 s, which suits web pages but not LLM calls.
- **RED metrics come from an ASGI middleware**, because only something wrapping the whole response sees a stream's real end. The endpoint passes the model label through `request.state`.
- **A provider dying mid-stream** is now visible as `outcome="mid_stream"` (the open item in G6). It still doesn't trip the breaker.
- **One process per container** for now. With several uvicorn workers in one container, each worker has its own counters, and you'd need the client library's *multiprocess mode*.
- **Not yet:** $ cost (needs prices in the config) and gateway overhead (the 20 ms target; it's measured in Phase 6 with a mock provider).
- **Production terms:** *cardinality*, *time series*, *bucket*, *state set*, *instrumentation*, *middleware*.
- **Read (only if needed):** Prometheus docs, "Metric types" and "Instrumentation: Use labels" (prometheus.io/docs/practices/instrumentation/).

### H4. Local monitoring stack: Docker Compose, dashboards as code
- **Decision:** `docker-compose.yml` runs Prometheus (scrapes `host.docker.internal:8000` every 15 s) and Grafana. Grafana's data source and both dashboards are **provisioned** from files in `monitoring/`, so `docker compose up -d` gives anyone the same dashboards with no clicking.
- **The gateway stays on the host for now** (fast edit-restart loop). It moves into Compose in Phase 3, alongside Redis.
- **Alternatives:** build dashboards by clicking in Grafana (lost when the container is recreated, and can't be reviewed in a PR); Grafana Cloud (hosted, but needs an account).
- **Queries worth knowing:** `rate(counter[1m])` turns a counter into per-second; `histogram_quantile(0.95, sum by (le) (rate(..._bucket[5m])))` gives p95; `increase(counter[1h])` gives a total over a window.
- **Prometheus history lives in a named volume** (`prometheus-data`), so it survives `docker compose down`; `down -v` wipes it. Containers are disposable: anything that must survive lives in a mounted file or a volume. Production adds a *retention period* (Prometheus default: 15 days).
- **Images pinned:** `prom/prometheus:v3.5.0`, `grafana/grafana:12.1.0`. Anonymous Viewer access is on for local use only.
- **Production terms:** *provisioning*, *dashboards as code*, *PromQL*, *service discovery* (replaces our static target in Kubernetes).

### H5. Alert on TTFT and TPOT, not just total latency
- **Decision:** add `gateway_tpot_seconds{deployment}` (histogram, buckets 5 ms–0.5 s per token), computed at the end of a stream as `(total − TTFT) ÷ (completion tokens − 1)`. Alongside TTFT, it's an on-call panel.
- **Why:** an LLM writes output **one token at a time**, so total latency depends mostly on **answer length**. A p95 jump from 2 s to 8 s may just mean one team started asking for essays. TTFT and TPOT don't depend on length, so a rise in either means the provider really is slow. (Input tokens matter much less: the model reads them all at once, which is called *prefill*.)
- **Cons:** streams only (a non-streamed answer arrives in one piece, so there's nothing to time per token); no TPOT if the client leaves before the usage chunk.
- **Bucket resolution:** histogram percentiles are estimates *inside* a bucket (the measured p50 1.29 s and p95 1.93 s were both somewhere in the 1–2 s bucket). Add buckets where the distinction matters.
- **Industry:** vLLM, TGI, and LLM benchmarking tools report TTFT and TPOT (also called *inter-token latency*, ITL) as their main latency numbers.
- **Production terms:** *TTFT*, *TPOT / ITL*, *prefill vs decode*, *tokens per second*, *bucket resolution*.

## I. More providers: Anthropic adapter, Groq, Ollama

### I1. Providers have a `type`; only Anthropic needs translation
- **Decision:** each provider in `config.yaml` has `type: openai` (the default) or `type: anthropic`. Anything that speaks OpenAI's API (Groq, Ollama, vLLM, Together, DeepSeek...) needs only config. Anthropic gets an adapter (`anthropic_adapter.py`) that translates the request on the way out and the response or stream on the way back.
- **Why:** routing, fallback, the breaker and metrics stay unchanged; only the "talk to this provider" step knows about Anthropic. This is the *adapter pattern*.
- **Alternatives:** Anthropic's own OpenAI-compatible endpoint (no code, but missing features and meant mainly for testing); Anthropic's Python SDK (a second HTTP stack with its own pool, timeouts and retries, which would bypass the timeout and retry policy in F).
- **Industry:** LiteLLM is essentially a large collection of these adapters, built on plain httpx.

### I2. Request mapping
- `system`/`developer` messages move to Anthropic's top-level `system` field. `stop` becomes `stop_sequences`, `user` becomes `metadata.user_id`, and `max_completion_tokens`/`max_tokens` become `max_tokens`.
- **`max_tokens` defaults to 16000** when the client leaves it out (Anthropic requires it). Thinking is on by default on current Claude models and its tokens count toward the cap, so a small default can cut the answer off. The cap doesn't change cost; you pay for tokens generated.
- **`temperature` and `top_p` are dropped**, and the response says so in `x-gateway-dropped-params`. Current Claude models only accept the default sampling, and API version `2026-10-21` rejects the fields outright. *(Changed from the first proposal, which was to cap temperature at 1: that would still get a 400.)* The cost is that a client asking for `temperature: 0` gets default sampling from Claude.
- **API version:** `anthropic-version: 2026-10-21`.

### I3. Features Anthropic can't handle: skip the target, don't blame anyone
- **Decision:** a request with fields the adapter doesn't translate (`tools`, `n` > 1, `logprobs`, `response_format`...) skips the Anthropic target with failure kind `unsupported` and moves to the next one. It **never counts against the breaker** (the provider was never called) and is never retried. If every target was unsupported, the client gets a 400 naming the field. If any target failed for real, that failure decides the status (a 503 from OpenAI is still a 502, not the client's fault).
- **Alternatives:** silently drop unknown fields (LiteLLM's `drop_params`; a request with `tools` would get an answer that ignores them); always 400 (wrong when another target could handle it).
- **v2:** translate tool calling.

### I4. Errors
- 401/403 → `auth`, 429 → `rate_limit`, 500 and **529 `overloaded_error`** → `server`: the same table as F. Anthropic's 4xx error bodies are rewritten into OpenAI's shape (`{"error": {"message", "type", "code"}}`) so clients only parse one format.

### I5. Streaming: translate events, commit at the first content block
- Anthropic's events (`message_start`, `content_block_start/delta/stop`, `message_delta`, `message_stop`, `ping`, `error`) become OpenAI chunks. Thinking blocks are left out. Input tokens (from `message_start`, including cache reads and writes) and output tokens (from `message_delta`) are combined into the final `usage` chunk, so metrics and cost tracking work unchanged.
- **Commit point:** nothing reaches the client until the model starts its first content block. Anthropic can send `message_start` and then report `overloaded_error` inside the 200 stream; because we haven't committed, that still falls back. *Production term:* *in-band errors*. TrueFoundry calls the same idea a "first-chunk gate".
- **Why the first content block, not the first text:** with thinking on, text can start long after our 10 s first-chunk timeout, which would cause needless fallbacks that cost money.
- **After the commit point** an `error` event, or a stream that ends without `message_stop`, sends our usual error event with no `[DONE]` (D-section).

### I6. Groq and Ollama: config only
- Groq (`https://api.groq.com/openai/v1`, `llama-3.1-8b-instant` / `llama-3.3-70b-versatile`) and Ollama need no code. Fallback chains pair by tier: `gpt-4o-mini` → Groq 8B → Claude Haiku 5.5 → local Llama; `gpt-4o` → Claude Sonnet 5.5 → Groq 70B.
- **Not verified for real yet:** no Groq or Anthropic key, and Ollama isn't installed. Their config entries are commented out because the gateway refuses to start when a configured key is missing (C-section). Tests use a fake Anthropic built from Anthropic's documented formats.
- **Read (only if needed):** Anthropic, "Streaming Messages" (platform.claude.com/docs/en/build-with-claude/streaming); "Migrating to API version 2026-10-21" (platform.claude.com/docs/en/api/migration-2026-10-21).
