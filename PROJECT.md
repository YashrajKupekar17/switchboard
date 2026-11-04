# LLM Gateway — Project Description

## 1. Problem

Teams that use LLMs in production run into the same problems again and again:

- **Provider outages and rate limits.** OpenAI or Anthropic return 429s or 5xx errors, or time out, and the application breaks.
- **Cost.** Every request goes to an expensive model, even when a cheap one would do. Identical questions get paid for again and again.
- **No control.** Any team or user can burn through the budget. Nothing enforces per-client limits.
- **No visibility.** Nobody can answer "what's our p95 latency, error rate, or spend per tenant today?"
- **Vendor lock-in.** Every application integrates with each provider's SDK directly.

## 2. Solution

A self-hosted **LLM gateway**: one **OpenAI-compatible** endpoint that sits in front of multiple model providers.
Clients change only their `base_url`. The gateway handles:

- auth and per-key rate limits and budgets
- exact-match and semantic response caching
- cost-aware routing (cheap model first, escalate to a stronger one when needed)
- retries, timeouts, circuit breaking, and fallback across providers
- streaming pass-through (SSE)
- metrics and dashboards

```python
# Client code: the only change is base_url and the key
client = OpenAI(base_url="http://localhost:8000/v1", api_key="gw-tenant-abc")
client.chat.completions.create(model="auto", messages=[...], stream=True)
```

## 3. Goals

1. Provide a drop-in OpenAI-compatible `/v1/chat/completions`, both streaming and non-streaming.
2. Stay up when a single provider goes down.
3. Reduce cost through caching and routing, and **measure** the reduction.
4. Enforce per-key request and token rate limits and dollar budgets.
5. Make the system observable with Prometheus metrics and a Grafana dashboard.
6. Publish load-test numbers that show the gateway's own overhead.

## 4. Non-goals (deliberately out of scope)

- A UI or admin dashboard. Configuration lives in a file.
- Kubernetes, Kafka, microservices. A single service plus Redis, run with Docker Compose.
- Embeddings, images, audio, or legacy completions endpoints. Chat only.
- Fine-tuning or hosting models (Ollama covers the local case).
- Multi-region deployment.

## 5. Functional requirements

| ID | Requirement |
|----|-------------|
| F1 | `POST /v1/chat/completions` accepts the OpenAI request schema and returns an OpenAI response schema. |
| F2 | `stream=true` returns SSE chunks in OpenAI format, whatever the upstream provider is. |
| F3 | Supports providers **OpenAI**, **Anthropic**, and **Ollama** through adapters. |
| F4 | Requests are authenticated with a gateway API key mapped to a tenant. |
| F5 | Per-key limits on **requests/min** and **tokens/min**. When exceeded, return `429` with a `Retry-After` header. |
| F6 | Per-key **$ budget** (for example, monthly). When exhausted, return `402` or `429` with a clear error. |
| F7 | **Exact cache**: identical normalized requests return the cached response. |
| F8 | **Semantic cache** (opt-in per request): similar prompts above a similarity threshold return the cached response. |
| F9 | **Fallback**: on a retryable error or timeout, try the next provider in a configured chain. |
| F10 | **Cascade routing**: with `model="auto"`, call the cheap model first, validate the output, and escalate to the strong model on failure. |
| F11 | `GET /metrics` exposes Prometheus metrics. `GET /healthz` reports liveness. |
| F12 | Every response includes headers such as `x-gateway-provider`, `x-gateway-cache: hit|miss`, and `x-request-id`. |

## 6. Non-functional requirements (initial targets, validated in Phase 6)

| Area | Target |
|------|--------|
| Gateway overhead (excluding the provider), cache miss | p95 < 20 ms |
| Cache hit latency | p95 < 10 ms |
| Throughput (single instance, mock provider) | ≥ 500 RPS without errors |
| Availability | Requests succeed while any one provider is fully down |
| Semantic cache precision | False-hit rate < 1% at the chosen threshold (measured) |
| Statelessness | Gateway instances hold no shared state; everything shared lives in Redis |
| Security | Provider keys are never logged or returned; caches are isolated per tenant |

## 7. Tech stack

| Concern | Choice |
|---------|--------|
| Language / API | Python 3.12, FastAPI, Pydantic v2 |
| HTTP client | httpx (async, pooled) |
| Shared state | Redis Stack (counters, cache, vector search) |
| Metrics | prometheus-client, Prometheus, Grafana |
| Testing | pytest, respx (HTTP mocking) |
| Load testing | Locust + a mock provider service |
| Packaging / CI | Docker Compose, GitHub Actions |

## 8. Milestones

Each phase must be **working, tested, and measured** before the next one starts.

| Phase | Scope | Done when |
|-------|-------|-----------|
| 0 | Skeleton: non-streaming passthrough to OpenAI, config, Docker Compose | The OpenAI SDK pointed at the gateway gets a valid response; one test passes |
| 1 | SSE streaming passthrough | Streaming works through the SDK; TTFT is measured |
| 2 | Provider adapters, timeouts, retries, circuit breaker, fallback, `/metrics` | Killing a provider mid-run causes no client errors; metrics are visible |
| 3 | API keys, rate limits (Redis, atomic), budgets | A concurrent test shows limits are never exceeded |
| 4 | Exact cache, then semantic cache | Hit-rate metric is live; a false-hit-vs-threshold curve is produced |
| 5 | Cascade routing with validation-based escalation | Escalation rate and cost saved are reported |
| 6 | Mock provider, Locust, Grafana dashboard, chaos test, README results | Numbers are published in the README |

## 9. Deliverables

- Source code, tests, and CI on GitHub
- `docker compose up` brings up the gateway, Redis, Prometheus, Grafana, and the mock provider
- `DESIGN.md`: architecture, decisions, and tradeoffs
- README with an architecture diagram, a Grafana screenshot, and a load-test and cache results table

## 10. Success metrics (for the README and resume)

- Gateway overhead at p50/p95/p99 and maximum RPS
- Cache hit rate and the resulting % cost reduction on a replayed workload
- Semantic cache false-hit rate at the chosen threshold
- Cascade escalation rate and the cost saved compared with always using the strong model
- Success rate during a simulated provider outage
