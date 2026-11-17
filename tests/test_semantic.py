import json
import math
import time

import httpx
import pytest
import respx

import budgets
import config
import main
import semantic
from cache import CacheSettings
from semantic import Match, lookup_for
from tests.test_cache import post, stored_keys, wait_for
from tests.test_gateway import FAKE_COMPLETION, OPENAI_URL

EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

# Hand-made unit vectors, so each pair's similarity is known: cos(a, b) = a . b
VECTORS = {
    "What's the capital of France?": [1.0, 0.0, 0.0],
    "capital of france": [0.98, 0.199, 0.0],  # 0.98 to the one above: the same question
    "How do I cancel my order?": [0.0, 1.0, 0.0],
    "How do I cancel my subscription?": [0.0, 0.93, 0.3676],  # 0.93: related, but a different question
}


def ask(question, **extra):
    return {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": question}], **extra}


class FakeVectors:
    """Stands in for Redis vector search (fakeredis has no FT.* commands): same methods, a dict and a cosine."""

    def __init__(self):
        self.entries = {}  # (partition, question) -> (vector, Hit)
        self.shadow = []
        self.down = False

    async def closest(self, partition, vector):
        if self.down:
            raise OSError("connection refused")
        scored = [(_cosine(v, vector), q, hit) for (p, q), (v, hit) in self.entries.items() if p == partition]
        if not scored:
            return None
        similarity, question, hit = max(scored, key=lambda s: s[0])
        return Match(similarity, question, f"sem:{partition}", hit)

    async def add(self, lookup, vector, body, deployment, ttl):
        self.entries[(lookup.partition, lookup.question)] = (vector, semantic.Hit(body, deployment, 0))

    async def save_shadow(self, team, record):
        self.shadow.append((team, record))


def _cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


# What the fake judge answers: the probability of "yes, the same question", per pair.
JUDGE_SAYS = {
    frozenset({"What's the capital of France?", "capital of france"}): 0.99,
    frozenset({"How do I cancel my order?", "How do I cancel my subscription?"}): 0.01,
}


class FakeOpenAI:
    """Chat completions and the judge share one URL: tell them apart by the logprobs only the judge asks for."""

    def __init__(self, judge_fails=False):
        self.chat = self.judged = 0
        self.judge_fails = judge_fails

    def __call__(self, request):
        body = json.loads(request.content)
        if not body.get("logprobs"):
            self.chat += 1
            return httpx.Response(200, json=FAKE_COMPLETION)
        self.judged += 1
        if self.judge_fails:
            raise httpx.ReadTimeout("the judge took longer than 1.5 s")
        prompt = body["messages"][0]["content"]
        p = next(v for pair, v in JUDGE_SAYS.items() if all(q in prompt for q in pair))
        return httpx.Response(200, json={"choices": [{"logprobs": {"content": [{"top_logprobs": [
            {"token": "yes", "logprob": math.log(p)}, {"token": "no", "logprob": math.log(1 - p)}]}]}}]})


def mock_openai(**kw):
    fake = FakeOpenAI(**kw)
    respx.post(OPENAI_URL).mock(side_effect=fake)
    return fake


def fake_embeddings(request):
    text = json.loads(request.content)["input"]
    return httpx.Response(200, json={"data": [{"embedding": VECTORS[text]}]})


@pytest.fixture
def vectors(client):
    fake = FakeVectors()
    main.app.state.semantic.vectors = fake
    return fake


def turn_on(monkeypatch, mode="on", threshold=0.70):
    monkeypatch.setitem(main.CONFIG.team_cache, "test-team",
                        CacheSettings(enabled=True, ttl=600, semantic=mode, threshold=threshold))


# --- what gets compared ---

def test_only_the_question_differs_inside_a_partition():
    a = lookup_for("t", ask("capital of france", temperature=0))
    b = lookup_for("t", ask("What's the capital of France?", temperature=0))
    assert a.partition == b.partition and a.question != b.question


@pytest.mark.parametrize("change", [
    {"temperature": 0.7}, {"model": "gpt-4o"}, {"tools": [{"type": "function"}]},
    {"messages": [{"role": "system", "content": "answer in French"}, {"role": "user", "content": "hi"}]},
    {"messages": [{"role": "user", "content": "hi", "name": "alice"}]},  # the last message's other fields too
])
def test_everything_else_must_match_exactly(change):
    base = ask("hi", temperature=0)
    assert lookup_for("t", base).partition != lookup_for("t", {**base, **change}).partition


def test_delivery_fields_are_ignored():
    assert lookup_for("t", ask("hi")).partition == lookup_for("t", ask("hi", stream=True, user="u-1")).partition


def test_teams_and_embedding_models_never_share_a_partition(monkeypatch):
    before = lookup_for("support", ask("hi")).partition
    assert before != lookup_for("ads", ask("hi")).partition
    monkeypatch.setattr(semantic, "EMBED_MODEL", "some-other-model")  # vectors from two models can't be compared
    assert before != lookup_for("support", ask("hi")).partition


@pytest.mark.parametrize("messages", [
    [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],  # not a user question
    [{"role": "user", "content": "x" * 2001}],  # too long: one changed sentence would still score ~0.99
    [{"role": "user", "content": "   "}],
    [],
])
def test_requests_that_cannot_use_it(messages):
    assert lookup_for("t", {"model": "m", "messages": messages}) is None


# --- the request path ---

@respx.mock
def test_a_paraphrase_is_served_from_the_cache(client, vectors, monkeypatch, ledger):
    turn_on(monkeypatch)
    respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    openai = mock_openai()

    first = post(client, ask("What's the capital of France?"), headers={})
    wait_for(lambda: vectors.entries)
    second = post(client, ask("capital of france"), headers={})

    assert openai.chat == 1
    assert first.headers["x-gateway-cache"] == "miss"
    assert second.headers["x-gateway-cache"] == "semantic-hit"
    assert second.headers["x-gateway-cache-similarity"] == "0.9800"
    assert second.headers["x-gateway-cache-judge"] == "0.99"
    assert openai.judged == 1
    assert second.json() == first.json()
    buffer = main.app.state.budgets.buffer
    wait_for(lambda: len(buffer) == 2)
    hit = dict(zip(budgets.COLUMNS, buffer[1]))
    assert hit["cache_hit"] and hit["cost_micros"] == 0  # same accounting as an exact hit


@respx.mock
def test_the_judge_rejects_a_related_but_different_question(client, vectors, monkeypatch):
    # 0.93 is over the threshold, so it's a candidate. The embedding can't tell; the judge reads both and says no.
    turn_on(monkeypatch)
    respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    openai = mock_openai()

    post(client, ask("How do I cancel my order?"), headers={})
    wait_for(lambda: vectors.entries)
    resp = post(client, ask("How do I cancel my subscription?"), headers={})

    assert resp.headers["x-gateway-cache"] == "miss"
    assert openai.judged == 1 and openai.chat == 2


@respx.mock
def test_under_the_threshold_the_judge_is_not_asked(client, vectors, monkeypatch):
    # The threshold decides who pays for a judge call: below it, a plain miss with no extra wait.
    turn_on(monkeypatch, threshold=0.95)
    respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    openai = mock_openai()

    post(client, ask("How do I cancel my order?"), headers={})
    wait_for(lambda: vectors.entries)
    resp = post(client, ask("How do I cancel my subscription?"), headers={})

    assert resp.headers["x-gateway-cache"] == "miss"
    assert openai.judged == 0


@respx.mock
def test_a_failing_judge_is_a_miss(client, vectors, monkeypatch):
    turn_on(monkeypatch)
    respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    openai = mock_openai(judge_fails=True)

    post(client, ask("What's the capital of France?"), headers={})
    wait_for(lambda: vectors.entries)
    resp = post(client, ask("capital of france"), headers={})

    assert resp.status_code == 200 and resp.headers["x-gateway-cache"] == "miss"
    assert openai.judged == 1 and openai.chat == 2


@respx.mock
def test_same_question_with_a_different_system_prompt_is_a_miss(client, vectors, monkeypatch):
    turn_on(monkeypatch)
    respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    openai = mock_openai()
    question = {"role": "user", "content": "What's the capital of France?"}

    post(client, {"model": "gpt-4o-mini", "messages": [question]}, headers={})
    wait_for(lambda: vectors.entries)
    resp = post(client, {"model": "gpt-4o-mini", "messages": [{"role": "system", "content": "be rude"}, question]},
                headers={})

    assert resp.headers["x-gateway-cache"] == "miss"
    assert openai.chat == 2


@respx.mock
def test_shadow_mode_records_the_would_be_hit_but_never_serves_it(client, vectors, monkeypatch):
    turn_on(monkeypatch, mode="shadow")
    respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    openai = mock_openai()

    post(client, ask("What's the capital of France?"), headers={})
    wait_for(lambda: vectors.entries)
    resp = post(client, ask("capital of france"), headers={})

    assert resp.headers["x-gateway-cache"] == "miss"  # the client sees nothing different
    assert openai.chat == 2
    wait_for(lambda: vectors.shadow)
    team, record = vectors.shadow[0]
    assert team == "test-team"
    assert record["similarity"] == 0.98
    assert record["question"] == "capital of france"
    assert record["matched_question"] == "What's the capital of France?"
    assert record["cached_answer"] == record["fresh_answer"] == "hello"
    assert record["judge"] == 0.99  # judged after the answer went back, so shadow mode never waits for it
    assert record["same"] is None  # for a person (or a judge) to label later


@respx.mock
def test_shadow_mode_keeps_pairs_under_the_threshold_too(client, vectors, monkeypatch):
    # Shadow mode is for picking a threshold, so it has to see what a lower one would serve.
    turn_on(monkeypatch, mode="shadow", threshold=0.95)
    respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    mock_openai()

    post(client, ask("How do I cancel my order?"), headers={})
    wait_for(lambda: vectors.entries)
    post(client, ask("How do I cancel my subscription?"), headers={})  # 0.93: under 0.95

    wait_for(lambda: vectors.shadow)
    assert vectors.shadow[0][1]["similarity"] == 0.93
    assert vectors.shadow[0][1]["judge"] == 0.01


@respx.mock
def test_exact_hit_comes_first_and_needs_no_embedding(client, vectors, monkeypatch):
    turn_on(monkeypatch)
    embeddings = respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    mock_openai()

    post(client, ask("capital of france"), headers={})
    wait_for(lambda: vectors.entries and stored_keys(client))
    resp = post(client, ask("capital of france"), headers={})

    assert resp.headers["x-gateway-cache"] == "hit"
    assert embeddings.call_count == 1  # only the first request


@respx.mock
def test_cache_off_for_a_request_turns_off_both(client, vectors, monkeypatch):
    turn_on(monkeypatch)
    embeddings = respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    mock_openai()

    post(client, ask("capital of france"), headers={"x-gateway-cache": "off"})

    assert embeddings.call_count == 0


# --- when things fail: always a miss, never an error ---

@respx.mock
def test_slow_embeddings_are_a_miss(client, vectors, monkeypatch):
    turn_on(monkeypatch)
    respx.post(EMBEDDINGS_URL).mock(side_effect=httpx.ReadTimeout("took longer than 500 ms"))
    openai = mock_openai()

    resp = post(client, ask("capital of france"), headers={})

    assert resp.status_code == 200 and resp.headers["x-gateway-cache"] == "miss"
    assert openai.chat == 1
    time.sleep(0.1)
    assert vectors.entries == {}  # nothing to store without a vector


@respx.mock
def test_breaker_stops_calling_a_failing_embeddings_api(client, vectors, monkeypatch):
    turn_on(monkeypatch)
    embeddings = respx.post(EMBEDDINGS_URL).mock(return_value=httpx.Response(500))
    mock_openai()

    statuses = [post(client, ask(f"question {i}"), headers={}).status_code for i in range(5)]

    assert statuses == [200] * 5
    assert embeddings.call_count == 3  # opened after 3 failures: the last 2 requests didn't wait for it


@respx.mock
def test_vector_store_down_is_a_miss(client, vectors, monkeypatch):
    turn_on(monkeypatch)
    vectors.down = True
    respx.post(EMBEDDINGS_URL).mock(side_effect=fake_embeddings)
    mock_openai()

    resp = post(client, ask("capital of france"), headers={})

    assert resp.status_code == 200 and resp.headers["x-gateway-cache"] == "miss"


# --- config ---

CONFIG_YAML = """
providers:
  openai: {base_url: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY}
  ollama: {base_url: http://ollama.test/v1}
models:
  local: [{provider: ollama, model: llama3.2}]
prices:
  ollama/llama3.2: {input: 0, output: 0}
cache:
  teams:
    search: %s
"""


@pytest.mark.parametrize("team, error", [
    ("{enabled: true, semantic: sometimes}", "semantic must be one of"),
    ("{enabled: true, semantic: on, threshold: 1.5}", "threshold must be"),
    ("{enabled: true, semantic: on, threshold: 0}", "threshold must be"),
])
def test_bad_semantic_settings_stop_startup(tmp_path, monkeypatch, team, error):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG_YAML % team)
    with pytest.raises(RuntimeError, match=error):
        config.load_config(str(path))


def test_bare_on_and_off_in_yaml_work(tmp_path, monkeypatch):
    # YAML reads a bare `on` as true and `off` as false. Both spellings should mean the mode, not crash startup.
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    path = tmp_path / "c.yaml"
    path.write_text(CONFIG_YAML % "{enabled: true, semantic: on}")
    assert config.load_config(str(path)).cache_for("search").semantic == "on"
    path.write_text(CONFIG_YAML % "{enabled: true, semantic: off}")
    assert config.load_config(str(path)).cache_for("search").semantic == "off"


def test_semantic_needs_the_openai_provider(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text((CONFIG_YAML % "{enabled: true, semantic: shadow}").replace(
        "  openai: {base_url: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY}\n", ""))
    with pytest.raises(RuntimeError, match="needs the 'openai' provider"):
        config.load_config(str(path))


# --- the Redis side (fakeredis has no vector search, so a stub that answers like Redis does) ---

class StubRedis:
    def __init__(self):
        self.indexes = set()
        self.creates = 0

    async def execute_command(self, *args):
        if args[0] == "FT.CREATE":
            self.creates += 1
            if args[1] in self.indexes:
                raise semantic.ResponseError("Index already exists")
            self.indexes.add(args[1])
            return b"OK"
        if args[0] == "FT.SEARCH":
            if args[1] not in self.indexes:
                raise semantic.ResponseError(f"No such index {args[1]}")
            return [0]


def test_a_lost_index_is_a_miss_not_an_outage():
    # FLUSHALL or a redis-cache restart deletes the index. The copy still thinks it exists.
    import asyncio
    class StubEmbedder:
        async def embed(self, text):
            return [1.0]

    stub = StubRedis()
    vectors = semantic.RedisVectors(stub)
    cache = semantic.SemanticCache(StubEmbedder(), None, vectors)
    lookup = semantic.Lookup("p", "q")

    async def run():
        assert await cache.find(lookup) == ([1.0], None)  # creates the index, finds nothing
        stub.indexes.clear()  # the flush
        assert await cache.find(lookup) == ([1.0], None)  # a miss with a vector, so the answer still gets stored
        assert not vectors.index_ready
        await vectors._ensure_index()  # what that store does first
        assert stub.creates == 2 and "semantic" in stub.indexes

    asyncio.run(run())
    assert cache.down_until == 0.0  # never treated as Redis being down
