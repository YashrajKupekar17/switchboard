"""Which models a team may ask for (DECISIONS.md T4)."""
from pathlib import Path

import httpx
import pytest
import respx
import yaml

import main
from config import load_config
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OPENAI_URL

TEST_CONFIG = Path(__file__).parent / "config.test.yaml"
TOOLS = [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]


def write_config(tmp_path, access):
    raw = yaml.safe_load(TEST_CONFIG.read_text())
    raw["access"] = access
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw))
    return str(path)


def test_a_team_not_listed_gets_the_default_and_star_means_any(tmp_path):
    cfg = load_config(write_config(tmp_path, {"default": {"models": ["gpt-4o-mini"]},
                                              "teams": {"research": {"models": "*"}}}))
    assert cfg.models_for("anyone") == {"gpt-4o-mini"}
    assert cfg.models_for("research") is None
    assert load_config(write_config(tmp_path, {})).models_for("anyone") is None  # no access section: any


@pytest.mark.parametrize("access, error", [
    ({"teams": {"x": {"models": ["gpt-5-typo"]}}}, "isn't a model or route"),
    ({"teams": {"x": {"models": ["auto"]}}}, "needs its default"),  # auto would pick a model x can't use
    ({"teams": {"x": {"models": []}}}, "a list of names"),
])
def test_config_mistakes_stop_startup(tmp_path, access, error):
    with pytest.raises(RuntimeError, match=error):
        load_config(write_config(tmp_path, access))


@pytest.fixture
def mini_only(monkeypatch):
    monkeypatch.setitem(main.CONFIG.team_models, "test-team", frozenset({"gpt-4o-mini", "auto"}))


@respx.mock
def test_a_model_the_team_may_not_use_is_403_and_costs_nothing(client, mini_only):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    refused = client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "gpt-4o"})
    allowed = client.post("/v1/chat/completions", json=CHAT_BODY)

    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "model_not_allowed"
    assert "it can use: auto, gpt-4o-mini" in refused.json()["error"]["message"]
    assert route.call_count == 1  # only the allowed request reached the provider
    assert allowed.headers["x-ratelimit-remaining-requests"] == "999"  # the refusal took no rate limit


def test_an_unknown_name_is_still_404_not_403(client, mini_only):
    assert client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "made-up"}).status_code == 404


@respx.mock
def test_auto_stays_on_the_default_when_the_team_may_not_use_the_upgrade(client, mini_only):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "auto", "tools": TOOLS})

    assert resp.status_code == 200
    assert resp.headers["x-gateway-routed-to"] == "gpt-4o-mini"
    assert resp.headers["x-gateway-route-reason"] == "upgrade_not_allowed"
    assert '"model":"gpt-4o-mini"' in route.calls.last.request.content.decode().replace(" ", "")


@respx.mock
def test_auto_still_upgrades_for_a_team_that_may(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json={**CHAT_BODY, "model": "auto", "tools": TOOLS})

    assert resp.headers["x-gateway-routed-to"] == "gpt-4o" and resp.headers["x-gateway-route-reason"] == "tools"
