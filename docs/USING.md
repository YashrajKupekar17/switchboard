# Using switchboard

This page is for developers on a team that calls LLMs through switchboard. It covers how to connect, what you can ask for, what the extra headers mean, and what to do with each error.

If your code already uses the OpenAI SDK, you change two things: the base URL and the key.

## 1. Get a key

Ask the platform team for a key for your team. They run `scripts/keys.py create <team> <name>` and give you a key that starts with `sbk_`. Ask for one key per service, so one can be revoked without breaking the others.

Treat it like any API key: keep it in an environment variable or a secret store, never in code. A revoked key stops working within 60 seconds.

Your key decides which team you are, and so which rate limits, budget and cache settings apply.

## 2. Connect

The gateway speaks OpenAI's Chat Completions API at `/v1/chat/completions`. Locally that's `http://localhost:8000/v1`.

**Python, OpenAI SDK:**

```python
import os
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key=os.environ["SWITCHBOARD_API_KEY"])

response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "What is a circuit breaker?"}],
)
print(response.choices[0].message.content)
```

**curl:**

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $SWITCHBOARD_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Hello"}]}'
```

**Streaming:**

```python
stream = client.chat.completions.create(model="gpt-4o-mini", messages=messages, stream=True)
for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="")
```

As with OpenAI, if you send `stream_options={"include_usage": True}`, the last chunk before `[DONE]` carries `usage` and an empty `choices` list. Without it you don't get that chunk. (The gateway always asks the provider for usage, to bill your team, and only passes it on if you asked.)

Images (content as a list of parts) and tool calling across several turns work the same as with OpenAI.

Only Chat Completions is available. There's no `/v1/models`, embeddings or Responses API, so `client.models.list()` returns 404.

## 3. Models you can ask for

| Name | What you get |
|---|---|
| `gpt-4o-mini` | OpenAI's gpt-4o-mini. Falls back to Groq's gpt-oss-20b, then a local llama3.2 |
| `gpt-4o` | OpenAI's gpt-4o. Falls back to Groq's gpt-oss-120b |
| `fast` | A cheap, fast model, whichever vendor is up. Same list as `gpt-4o-mini` today |
| `strong` | A capable model, whichever vendor is up. Same list as `gpt-4o` today |
| `auto` | The gateway picks `fast` or `strong` from your request (below) |

Any other name gets a 404 with code `model_not_found`. The platform team can add models in `config.yaml`.

**Fallback.** If the first provider is down, failing, rate-limiting us or very slow, your request goes to the next one on the list without you doing anything. The answer may then come from a different model. The response body's `model` field and the `x-gateway-provider` header say what actually answered. If you need exactly the first model or an error, send `x-gateway-fallback: off`.

**`auto`.** You get `fast` unless your request offers tools, has 6 or more user messages, or has a prompt over about 3,000 tokens; then you get `strong`. The headers `x-gateway-routed-to` and `x-gateway-route-reason` (`default`, `tools`, `turns` or `long_prompt`) say what was picked and why. These rules are starting guesses, not tuned yet.

## 4. Headers you can send

| Header | Values | What it does |
|---|---|---|
| `x-gateway-fallback` | `off` | Only try the first provider. You get its answer or its error |
| `x-gateway-cache` | `on` or `off` | Use the cache for this request, or skip it, whatever your team's default is |
| `x-gateway-cache-ttl` | seconds, 1 to 604800 | How long to keep this answer in the cache. The default is 1 hour |

## 5. Headers you get back

| Header | Example | Meaning |
|---|---|---|
| `x-gateway-provider` | `openai` | Which provider answered |
| `x-gateway-attempts` | `openai:timeout` | What failed before the answer came. Only present if something did |
| `x-gateway-routed-to` | `fast` | `auto` only: the model it picked |
| `x-gateway-route-reason` | `tools` | `auto` only: why |
| `x-gateway-cache` | `off`, `miss`, `hit`, `semantic-hit` | What the cache did |
| `x-gateway-cache-similarity` | `0.8683` | Semantic hits only: how close the stored question was |
| `x-gateway-cache-judge` | `1.00` | Semantic hits only: how sure the judge was that it's the same question |
| `x-gateway-dropped-params` | `logit_bias` | Fields the answering provider doesn't support and we left out |
| `x-ratelimit-limit-requests`, `x-ratelimit-remaining-requests` | `60`, `57` | Your team's requests per minute |
| `x-ratelimit-limit-tokens`, `x-ratelimit-remaining-tokens` | `100000`, `98200` | Your team's tokens per minute |
| `x-gateway-budget-spent-usd`, `-soft-usd`, `-max-usd` | `12.40`, `50.00`, `60.00` | Your team's spend this month, its warning level and its hard limit |

The rate-limit headers are the same ones OpenAI sends, so code that already reads them keeps working.

## 6. Errors, and what to do

Errors come back in OpenAI's shape, `{"error": {"message", "type", "code"}}`, so the SDK raises its usual exceptions. When every provider failed, `error.attempts` lists what happened at each one.

| Status | `code` or `type` | What happened | What to do |
|---|---|---|---|
| 401 | `missing_api_key`, `invalid_api_key` | No key, a wrong one, or a revoked one | Check the key. Don't retry |
| 404 | `model_not_found` | The model name isn't configured | Use a name from section 3 |
| 400 | from the provider | Your request was invalid (bad parameter, too long for the model) | Fix the request. It's passed through as the provider sent it, and never tried elsewhere |
| 400 | `invalid_request_error` | No provider for this model supports a feature you used | Remove the feature or use another model |
| 400 | `invalid_request_error`, with `param` | The gateway couldn't read the request: no `model`, `messages` not a list | Fix the request |
| 429 | `rate_limit_exceeded` | Your team is over its requests or tokens per minute | Wait `Retry-After` seconds and retry. The OpenAI SDK does this for you |
| 429 | `rate_limit_exceeded`, message "request too large" | This one request needs more tokens than your team's whole per-minute limit | Retrying won't help. Shorten it, or ask for a higher limit |
| 429 | `budget_exceeded` | Your team has spent its monthly budget | Don't retry: it resets on the 1st (UTC). Talk to the platform team |
| 502 | `upstream_error` | Every provider failed | Retry later. `error.attempts` says why |
| 503 | `upstream_error`, with `Retry-After` | Providers are rate-limiting us, or all are known to be down | Retry after `Retry-After` |
| 503 | `gateway_overloaded` | This gateway copy is at capacity | Retry after 1 s; you'll probably reach a less busy copy |
| 503 | `auth_unavailable` | The key store is unreachable. The gateway refuses rather than let unknown keys in | Retry shortly |
| 504 | `upstream_error` | The last provider tried timed out | Retry later |

**A stream that breaks in the middle.** Once a stream has started sending tokens, it can't move to another provider. If the provider dies mid-answer, the stream ends with an error event, `data: {"error": {"message": "upstream provider failed mid-stream", ...}}`, and the OpenAI SDK raises `APIError`. You've received part of an answer. Throw it away and retry the request.

## 7. Rate limits and budgets

Each team has two limits per minute: **requests** and **tokens** (default 60 and 100,000; the platform team can set yours). A team can burst up to a minute's worth at once, then gets a steady rate.

Tokens are estimated from your prompt before the call, so a request can be refused before it costs anything. Once the answer arrives, the estimate is replaced by the real count. Long answers can take you briefly below zero; the next requests wait until it's paid back.

Each team also has a **monthly budget** in dollars, priced by the model that actually answered:
- Past the **soft** level, the platform team gets an alert. Your requests still work, and the `x-gateway-budget-*` headers show where you are.
- At the **max** level, every request gets a 429 `budget_exceeded` until the 1st of next month (UTC).

Cache hits cost nothing against your budget, but still count as a request for the per-minute limit.

## 8. The cache

The cache is **off unless your team turns it on**, in `config.yaml` or with `x-gateway-cache: on` per request. It's off by default because a cached answer to "write me a poem" would stop you ever getting a new poem.

**Exact cache.** The same request from your team (same model, messages and parameters) gets the stored answer in a few milliseconds, at no cost. Teams never see each other's entries. Only complete answers from the first-choice model are stored, never errors, cut-off answers or fallback answers.

**Semantic cache.** Your team can also let a question that *means* the same get the stored answer: "When are you open?" for "What are your opening hours?". An embedding finds the closest stored question, and gpt-4o-mini reads both and decides whether the same answer is right for both. Only the last message is compared, it has to be a user message under 2,000 characters, and everything else in the request has to match exactly.

**When to turn it on:**
- Good for answers that don't depend on who's asking: FAQs, documentation questions, support answers.
- Bad for creative or varied output, and for anything about a specific account, order or person. In our replay of 1,000 support questions, about 1 in 50 semantic hits answered a neighbouring question, and twice a question about one invoice number got the answer written for another (DECISIONS.md S3).
- Start with `semantic: shadow`. It looks for matches and saves them for review without serving any, so you can measure it on your own traffic first.

`x-gateway-cache: off` on a request skips both caches.

## 9. What doesn't work yet

- **Images count as a fixed 765 tokens** toward your per-minute token limit before the call (a 1024×1024 image at high detail). The real count from the provider replaces the estimate afterwards, as it does for text.
- **Only `/v1/chat/completions`.** No model listing, embeddings, audio, images or the Responses API.
- **Anthropic models (off in the default config) refuse what they can't translate**, such as tools or `n` above 1. With other models in the list, such a request goes to one of them; if an Anthropic model is the only one left, you get a 400.

`http://localhost:8000/docs` lists the endpoint and its request format, generated from the code.
