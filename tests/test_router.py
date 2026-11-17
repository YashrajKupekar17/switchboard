import json

import httpx
import pytest
import respx
import yaml

import main
from config import load_config
from router import Route, Rules, choose
from tests.test_cache import wait_for
from tests.test_gateway import FAKE_COMPLETION, OPENAI_URL

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
