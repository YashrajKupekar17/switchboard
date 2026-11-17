import dataclasses
import json
import time

import httpx
import pytest
import respx
import yaml
from prometheus_client import REGISTRY

import main
from config import load_config
from router import (SHADOW_MAX_TOKENS, Route, Rules, choose, render_answer, shadow_record, should_shadow,
                    verdict)
from tests.test_cache import FAKE_SSE as COMPLETE_SSE, wait_for
from tests.test_gateway import FAKE_COMPLETION, OLLAMA_URL, OPENAI_URL

ROUTE = Route("auto", "small", "big", Rules(has_tools=True, min_turns=3, min_prompt_tokens=1000))


def ask(text="hi", turns=1, **extra):
    messages = []
    for i in range(turns):
        messages += [{"role": "user", "content": text}, {"role": "assistant", "content": "ok"}]
    return {"model": "auto", "messages": messages[:-1], **extra}


# --- the rules ---

def test_easy_request_gets_the_default():
    assert choose(ROUTE, ask(), 10) == ("small", "default")


@pytest.mark.parametrize("payload, tokens, reason", [
    (ask(tools=[{"type": "function", "function": {"name": "f"}}]), 10, "tools"),
    (ask(functions=[{"name": "f"}]), 10, "tools"),  # the older name for tools
    (ask(turns=3), 10, "turns"),
    (ask(), 1000, "long_prompt"),
])
def test_any_rule_upgrades(payload, tokens, reason):
    assert choose(ROUTE, payload, tokens) == ("big", reason)


def test_just_under_every_rule_stays_on_the_default():
    assert choose(ROUTE, ask(turns=2, tools=[]), 999) == ("small", "default")


def test_only_user_messages_count_as_turns():
    payload = {"messages": [{"role": "system", "content": "x"}] * 5 + [{"role": "user", "content": "hi"}]}
    assert choose(ROUTE, payload, 10) == ("small", "default")


def test_a_rule_can_be_switched_off():
    route = Route("auto", "small", "big", Rules(has_tools=False, min_turns=None, min_prompt_tokens=None))
    assert choose(route, ask(turns=50, tools=[{"type": "function"}]), 10**6) == ("small", "default")


# --- the config ---

def write_config(tmp_path, routes):
    raw = yaml.safe_load(open("tests/config.test.yaml"))
    raw["routes"] = routes
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


def test_config_loads_routes_with_default_rules(tmp_path):
    cfg = load_config(write_config(tmp_path, {"auto": {"default": "gpt-4o-mini", "upgrade_to": "gpt-4o"}}))
    assert cfg.routes["auto"].rules == Rules()


@pytest.mark.parametrize("route, error", [
    ({"default": "nope", "upgrade_to": "gpt-4o"}, "default must be a model"),
    ({"default": "gpt-4o-mini", "upgrade_to": "nope"}, "upgrade_to must be a model"),
])
def test_config_rejects_unknown_models(tmp_path, route, error):
    with pytest.raises(RuntimeError, match=error):
        load_config(write_config(tmp_path, {"auto": route}))


def test_a_route_cannot_shadow_a_model_name(tmp_path):
    with pytest.raises(RuntimeError, match="already a model name"):
        load_config(write_config(tmp_path, {"gpt-4o": {"default": "gpt-4o-mini", "upgrade_to": "gpt-4o"}}))


# --- through the gateway ---

@respx.mock
def test_auto_calls_the_chosen_model_and_says_why(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    easy = client.post("/v1/chat/completions", json=ask())
    hard = client.post("/v1/chat/completions", json=ask(tools=[{"type": "function", "function": {"name": "f"}}]))

    assert [json.loads(c.request.content)["model"] for c in route.calls] == ["gpt-4o-mini", "gpt-4o"]
    assert (easy.headers["x-gateway-routed-to"], easy.headers["x-gateway-route-reason"]) == ("gpt-4o-mini", "default")
    assert (hard.headers["x-gateway-routed-to"], hard.headers["x-gateway-route-reason"]) == ("gpt-4o", "tools")


@respx.mock
def test_decisions_are_counted_per_team_and_reason(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    labels = {"team": "test-team", "route": "auto", "model": "gpt-4o", "reason": "turns"}
    before = REGISTRY.get_sample_value("gateway_route_decisions_total", labels) or 0

    client.post("/v1/chat/completions", json=ask(turns=3))

    assert REGISTRY.get_sample_value("gateway_route_decisions_total", labels) == before + 1


@respx.mock
def test_asking_for_a_model_by_name_is_never_routed(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json={**ask(turns=5), "model": "gpt-4o-mini"})

    assert json.loads(route.calls.last.request.content)["model"] == "gpt-4o-mini"
    assert "x-gateway-routed-to" not in resp.headers


@respx.mock
def test_auto_is_charged_at_the_chosen_models_price(client, ledger):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    client.post("/v1/chat/completions", json=ask(turns=3))

    def written():
        client.portal.call(main.app.state.budgets.flush)  # usage is settled in the background, then flushed
        return ledger.rows

    wait_for(written)
    assert {"auto", "openai/gpt-4o"} <= set(ledger.rows[-1])  # what was asked for, and what answered and is charged


# --- shadow mode ---

def test_shadow_sampling():
    route = Route("auto", "small", "big", Rules(), shadow_sample=0.1)
    assert should_shadow(route, 10, rand=lambda: 0.05)
    assert not should_shadow(route, 10, rand=lambda: 0.5)
    assert not should_shadow(route, SHADOW_MAX_TOKENS + 1, rand=lambda: 0.0)  # too big to keep
    assert not should_shadow(Route("auto", "small", "big", Rules()), 10, rand=lambda: 0.0)  # off by default


@pytest.mark.parametrize("served", ["small", "big"])
def test_shadow_record_knows_which_answer_is_which(served):
    def body(text):
        return {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}

    route = Route("auto", "small", "big", Rules())
    served_text, other_text = ("from small", "from big") if served == "small" else ("from big", "from small")
    record = shadow_record(route, "t", "default", served, ask(), body(served_text), body(other_text), "now")
    assert record["cheap_answer"] == {"content": "from small"}
    assert record["strong_answer"] == {"content": "from big"}
    assert record["verdict"] is None


def answers_by_model(request):
    model = json.loads(request.content)["model"]
    return httpx.Response(200, json={**FAKE_COMPLETION, "choices": [
        {"index": 0, "message": {"role": "assistant", "content": f"answer from {model}"}, "finish_reason": "stop"}]})


@pytest.fixture
def shadow_on(monkeypatch):
    monkeypatch.setitem(main.CONFIG.routes, "auto", dataclasses.replace(main.CONFIG.routes["auto"], shadow_sample=1))


def shadow_records(client):
    return client.portal.call(main.app.state.route_shadow.records, "test-team")


@respx.mock
def test_shadow_asks_the_other_model_after_answering(client, ledger, shadow_on):
    route = respx.post(OPENAI_URL).mock(side_effect=answers_by_model)

    resp = client.post("/v1/chat/completions", json=ask())

    assert resp.json()["choices"][0]["message"]["content"] == "answer from gpt-4o-mini"  # the client's answer
    wait_for(lambda: shadow_records(client))
    record = shadow_records(client)[0]
    assert [json.loads(c.request.content)["model"] for c in route.calls] == ["gpt-4o-mini", "gpt-4o"]
    assert (record["served"], record["reason"]) == ("gpt-4o-mini", "default")
    assert record["cheap_answer"] == {"content": "answer from gpt-4o-mini"}
    assert record["strong_answer"] == {"content": "answer from gpt-4o"}

    def written():
        client.portal.call(main.app.state.budgets.flush)
        return ledger.rows

    wait_for(written)
    assert [r for r in ledger.rows if "openai/gpt-4o" in r] == []  # the platform pays for shadow answers


@respx.mock
def test_shadow_works_for_streams(client, shadow_on):
    def answer(request):
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, content=COMPLETE_SSE, headers={"content-type": "text/event-stream"})
        return answers_by_model(request)

    route = respx.post(OPENAI_URL).mock(side_effect=answer)

    client.post("/v1/chat/completions", json=ask(stream=True))

    wait_for(lambda: shadow_records(client))
    shadow_call = json.loads(route.calls.last.request.content)
    assert "stream" not in shadow_call and "stream_options" not in shadow_call  # one whole answer to compare
    assert shadow_records(client)[0]["cheap_answer"] == {"content": "Hi"}


@respx.mock
def test_a_failed_shadow_call_saves_nothing_and_the_client_never_knows(client, shadow_on):
    respx.post(OPENAI_URL).mock(side_effect=[httpx.Response(200, json=FAKE_COMPLETION), httpx.Response(500)])
    failed = {"team": "test-team", "result": "failed"}
    before = REGISTRY.get_sample_value("gateway_route_shadow_total", failed) or 0

    resp = client.post("/v1/chat/completions", json=ask())

    assert resp.status_code == 200
    wait_for(lambda: REGISTRY.get_sample_value("gateway_route_shadow_total", failed) == before + 1)
    assert shadow_records(client) == []


@respx.mock
def test_no_shadow_for_a_fallback_answer(client, shadow_on):
    openai = respx.post(OPENAI_URL).mock(return_value=httpx.Response(503))
    respx.post(OLLAMA_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json=ask())

    assert resp.status_code == 200
    time.sleep(0.1)
    assert openai.call_count == 1 and shadow_records(client) == []  # not the model the route picked: nothing to compare


@respx.mock
def test_no_shadow_when_sampling_is_off(client):
    route = respx.post(OPENAI_URL).mock(side_effect=answers_by_model)

    client.post("/v1/chat/completions", json=ask())

    time.sleep(0.1)
    assert route.call_count == 1 and shadow_records(client) == []


# --- the offline judge ---

@pytest.mark.parametrize("strong_first, cheap_first, expected", [
    ("tie", "tie", "cheap_as_good"),
    ("2", "1", "cheap_as_good"),  # the cheap answer won in both orders
    ("tie", "1", "cheap_as_good"),
    ("1", "2", "strong_better"),
    ("1", "1", "unsure"),  # picked whichever came first both times: position bias, not a verdict
    ("2", "2", "unsure"),
    ("1", "tie", "unsure"),
])
def test_only_a_verdict_both_orders_agree_on_counts(strong_first, cheap_first, expected):
    assert verdict(strong_first, cheap_first) == expected


def test_tool_calls_are_shown_to_the_judge():
    rendered = render_answer({"tool_calls": [{"function": {"name": "get_weather", "arguments": '{"city":"Paris"}'}}]})
    assert "get_weather" in rendered and "Paris" in rendered


@respx.mock
def test_no_shadow_when_the_served_answer_was_cut_off(client, shadow_on):
    cut_off = {**FAKE_COMPLETION, "choices": [{**FAKE_COMPLETION["choices"][0], "finish_reason": "length"}]}
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=cut_off))

    client.post("/v1/chat/completions", json=ask())

    time.sleep(0.1)
    assert route.call_count == 1 and shadow_records(client) == []  # half an answer isn't a fair comparison
