from decimal import Decimal

import pytest

import config
from anthropic_adapter import from_anthropic
from pricing import Price, cost_micros, micros_to_usd, split_usage

MINI = Price.from_config({"input": 0.15, "cached_input": 0.075, "output": 0.60})
HAIKU = Price.from_config({"input": 0.10, "cached_input": 0.01, "cache_write": 0.125, "output": 0.50})


def test_input_and_output_are_priced_separately():
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    assert cost_micros(MINI, usage) == 450  # 1000 × 0.15 + 500 × 0.60 micro-dollars


def test_cached_prompt_tokens_are_cheaper():
    usage = {"prompt_tokens": 1000, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 800}}
    assert cost_micros(MINI, usage) == 90  # 200 × 0.15 + 800 × 0.075


def test_anthropic_cache_writes_cost_more_than_input():
    usage = from_anthropic({
        "id": "msg_1", "model": "claude-haiku-5-5", "content": [], "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "cache_read_input_tokens": 1000, "cache_creation_input_tokens": 2000,
                  "output_tokens": 10},
    })["usage"]
    tokens = split_usage(usage)
    assert (tokens.uncached, tokens.cached, tokens.cache_write, tokens.output) == (100, 1000, 2000, 10)
    assert cost_micros(HAIKU, usage) == 275  # 100×0.10 + 1000×0.01 + 2000×0.125 + 10×0.50


def test_rounds_once_per_request_to_the_nearest_micro_dollar():
    assert cost_micros(MINI, {"prompt_tokens": 6, "completion_tokens": 0}) == 1  # 0.9 µ$


def test_prices_stay_exact():
    assert Price.from_config({"input": 0.1, "output": 0.2}).input == Decimal("0.1")  # not 0.1000000000000000055...
    assert micros_to_usd(450) == "0.000450"


def test_no_usage_costs_nothing():
    assert cost_micros(MINI, None) == 0


def test_missing_price_stops_startup(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    path = tmp_path / "c.yaml"
    path.write_text("""
providers:
  openai: {base_url: https://api.openai.com/v1, api_key_env: OPENAI_API_KEY}
models:
  gpt-4o-mini: [{provider: openai, model: gpt-4o-mini}]
prices: {}
""")
    with pytest.raises(RuntimeError, match="no price for 'openai/gpt-4o-mini'"):
        config.load_config(str(path))
