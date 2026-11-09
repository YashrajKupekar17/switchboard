import os
from dataclasses import dataclass
from typing import Optional

import yaml

from ratelimit import Limits

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

    def limits_for(self, team: str) -> Limits:
        return self.team_limits.get(team, self.default_limits)


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

    rl = raw.get("rate_limits", {})
    default = rl.get("default", {"rpm": 60, "tpm": 100_000})
    return Config(
        providers=providers,
        models=models,
        default_limits=Limits(**default),
        team_limits={team: Limits(**{**default, **l}) for team, l in (rl.get("teams") or {}).items()},
        gateway_copies=rl.get("gateway_copies", 1),
    )
