import os
from dataclasses import dataclass
from typing import Optional

import yaml

from budgets import Budget
from cache import MAX_TTL, CacheSettings
from pricing import Price
from ratelimit import Limits
from semantic import MODES as SEMANTIC_MODES

PROVIDER_TYPES = ("openai", "anthropic")  # "openai" = anything that speaks OpenAI's API (OpenAI, Groq, Ollama, vLLM...)
ANTHROPIC_VERSION = "2026-10-21"


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: Optional[str]
    type: str = "openai"

    @property
    def chat_url(self) -> str:
        path = "messages" if self.type == "anthropic" else "chat/completions"
        return f"{self.base_url.rstrip('/')}/{path}"

    def headers(self) -> dict:
        if self.type == "anthropic":
            return {"x-api-key": self.api_key or "", "anthropic-version": ANTHROPIC_VERSION}
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


@dataclass(frozen=True)
class Target:
    """One place to try a request: which provider, and that provider's name for the model."""

    provider: Provider
    model: str

    @property
    def deployment(self) -> str:
        return f"{self.provider.name}/{self.model}"  # e.g. "openai/gpt-4o-mini": the breaker and metrics name


@dataclass(frozen=True)
class Config:
    providers: dict[str, Provider]
    models: dict[str, list[Target]]  # client-facing model name -> ordered targets
    default_limits: Limits
    team_limits: dict[str, Limits]
    gateway_copies: int  # used only to split limits while Redis is down
    prices: dict[str, Price]  # deployment ("openai/gpt-4o-mini") -> price
    default_budget: Budget
    team_budgets: dict[str, Budget]
    default_cache: CacheSettings
    team_cache: dict[str, CacheSettings]

    def limits_for(self, team: str) -> Limits:
        return self.team_limits.get(team, self.default_limits)

    def budget_for(self, team: str) -> Budget:
        return self.team_budgets.get(team, self.default_budget)

    def cache_for(self, team: str) -> CacheSettings:
        return self.team_cache.get(team, self.default_cache)


def _yaml_modes(cache: dict) -> dict:
    # YAML reads a bare `on` as true and `off` as false, so `semantic: on` arrives as True.
    semantic = cache.get("semantic")
    if isinstance(semantic, bool):
        return {**cache, "semantic": "on" if semantic else "off"}
    return cache


def load_config(path: str) -> Config:
    with open(path) as f:
        raw = yaml.safe_load(f)

    providers = {}
    for name, p in raw["providers"].items():
        api_key = None
        if "api_key_env" in p:
            api_key = os.environ.get(p["api_key_env"])
            if not api_key:
                # Fail at startup, not on the first request at 3am.
                raise RuntimeError(f"provider {name!r}: env var {p['api_key_env']} is not set")
        ptype = p.get("type", "openai")
        if ptype not in PROVIDER_TYPES:
            raise RuntimeError(f"provider {name!r}: unknown type {ptype!r}, expected one of {PROVIDER_TYPES}")
        providers[name] = Provider(name=name, base_url=p["base_url"], api_key=api_key, type=ptype)

    models = {}
    for alias, targets in raw["models"].items():
        for t in targets:
            if t["provider"] not in providers:
                raise RuntimeError(f"model {alias!r}: unknown provider {t['provider']!r}")
        models[alias] = [Target(provider=providers[t["provider"]], model=t["model"]) for t in targets]

    prices = {deployment: Price.from_config(p) for deployment, p in (raw.get("prices") or {}).items()}
    for targets in models.values():
        for t in targets:
            if t.deployment not in prices:
                # Without a price its spend would silently count as $0 and no budget could stop it.
                raise RuntimeError(f"no price for {t.deployment!r}: add it under `prices` in the config")

    b = raw.get("budgets", {})
    default_budget = b.get("default", {"soft_usd": 50, "max_usd": 60})
    budgets = {team: Budget(**{**default_budget, **v}) for team, v in (b.get("teams") or {}).items()}
    for team, budget in [("default", Budget(**default_budget)), *budgets.items()]:
        if budget.max_usd < budget.soft_usd:
            raise RuntimeError(f"budget for {team!r}: max_usd must be at least soft_usd")

    c = raw.get("cache", {})
    default_cache = _yaml_modes({"enabled": False, "ttl": 3600, **(c.get("default") or {})})  # off unless a team asks (N1)
    team_cache = {team: CacheSettings(**_yaml_modes({**default_cache, **v})) for team, v in (c.get("teams") or {}).items()}
    for team, settings in [("default", CacheSettings(**default_cache)), *team_cache.items()]:
        if not 1 <= settings.ttl <= MAX_TTL:
            raise RuntimeError(f"cache for {team!r}: ttl must be between 1 and {MAX_TTL} seconds")
        if settings.semantic not in SEMANTIC_MODES:
            raise RuntimeError(f"cache for {team!r}: semantic must be one of {SEMANTIC_MODES}")
        if not 0 < settings.threshold <= 1:
            raise RuntimeError(f"cache for {team!r}: threshold must be above 0 and at most 1")
        if settings.semantic != "off" and "openai" not in providers:
            # Questions are embedded with OpenAI's API (DECISIONS.md O4), so it needs the openai provider's key.
            raise RuntimeError(f"cache for {team!r}: the semantic cache needs the 'openai' provider configured")

    rl = raw.get("rate_limits", {})
    default = rl.get("default", {"rpm": 60, "tpm": 100_000})
    return Config(
        providers=providers,
        models=models,
        default_limits=Limits(**default),
        team_limits={team: Limits(**{**default, **l}) for team, l in (rl.get("teams") or {}).items()},
        gateway_copies=rl.get("gateway_copies", 1),
        prices=prices,
        default_budget=Budget(**default_budget),
        team_budgets=budgets,
        default_cache=CacheSettings(**default_cache),
        team_cache=team_cache,
    )
