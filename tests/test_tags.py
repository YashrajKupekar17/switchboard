"""Request tags (DECISIONS.md T1): parsed strictly, stored with each request, never part of the cache key."""
import datetime as dt
import json

import httpx
import pytest
import respx

import budgets
import tags
from scripts.spend import build_query
from tests.test_cache import wait_for
from tests.test_gateway import CHAT_BODY, FAKE_COMPLETION, OPENAI_URL


def test_parses_name_value_pairs():
    assert tags.parse("feature=search, env=prod,ticket=JIRA-12") == {"feature": "search", "env": "prod",
                                                                     "ticket": "JIRA-12"}
    assert tags.parse(None) == {} and tags.parse("  ") == {}


@pytest.mark.parametrize("header", [
    "feature",                # no value
    "Feature=search",         # names are lowercase, so a report never has "Env" and "env"
    "feature=has space",
    "feature=a,feature=b",    # twice
    "x=" + "v" * 65,          # value too long
    ",".join(f"t{i}=v" for i in range(11)),  # too many
])
def test_refuses_anything_else(header):
    with pytest.raises(tags.BadTags):
        tags.parse(header)


def rows(client, expected):
    """The usage rows, once `expected` of them are in: they're written in the background after the response."""
    buffer = client.app.state.budgets.buffer
    wait_for(lambda: len(buffer) >= expected)
    return [dict(zip(budgets.COLUMNS, r)) for r in buffer]


@respx.mock
def test_tags_are_stored_with_the_request_and_with_cache_hits(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))
    headers = {"x-gateway-cache": "on"}

    client.post("/v1/chat/completions", json=CHAT_BODY, headers={**headers, "x-gateway-tags": "feature=search"})
    hit = client.post("/v1/chat/completions", json=CHAT_BODY, headers={**headers, "x-gateway-tags": "feature=faq"})

    assert hit.headers["x-gateway-cache"] == "hit"  # tags aren't in the cache key: same question, same answer
    first, second = rows(client, 2)
    assert json.loads(first["tags"]) == {"feature": "search"}
    assert json.loads(second["tags"]) == {"feature": "faq"} and second["cache_hit"]


@respx.mock
def test_no_tags_is_null_not_an_empty_object(client):
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    client.post("/v1/chat/completions", json=CHAT_BODY)

    assert rows(client, 1)[0]["tags"] is None


@respx.mock
def test_bad_tags_are_refused_before_anything_is_spent(client):
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=FAKE_COMPLETION))

    resp = client.post("/v1/chat/completions", json=CHAT_BODY, headers={"x-gateway-tags": "Feature=Search"})

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "invalid_tags"
    assert not route.called and client.app.state.budgets.buffer == []


def test_report_query_takes_only_known_columns_and_tag_names_as_parameters():
    sql, params = build_query(["team", "tag:feature"], dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc), team="search")

    assert "l.tags ->> $3" in sql and params[2] == "feature"  # the tag name is a parameter, not part of the SQL
    assert "l.team = $4" in sql and params[3] == "search"
    assert params[1] == dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
    for bad in (["tag:x'); DROP TABLE usage_ledger; --"], ["cost_micros"]):
        with pytest.raises(ValueError):
            build_query(bad, dt.datetime(2026, 11, 1, tzinfo=dt.timezone.utc))
