import os
from dataclasses import dataclass
from typing import Optional

import yaml


@dataclass(frozen=True)
class Provider:
    name: str
    base_url: str
    api_key: Optional[str]

    @property
    def chat_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def headers(self) -> dict:
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
        providers[name] = Provider(name=name, base_url=p["base_url"], api_key=api_key)

    models = {}
    for alias, targets in raw["models"].items():
        for t in targets:
            if t["provider"] not in providers:
                raise RuntimeError(f"model {alias!r}: unknown provider {t['provider']!r}")
        models[alias] = [Target(provider=providers[t["provider"]], model=t["model"]) for t in targets]

    return Config(providers=providers, models=models)
