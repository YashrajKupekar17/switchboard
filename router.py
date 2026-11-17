"""Model routing: a client asks for a route ("auto") instead of a model, and the gateway picks one. Decisions: DECISIONS.md P.

    request for "auto" -> look at the request only (no model is called) -> the route's default model,
                          or its upgrade model if any rule says the request looks hard

Rules, not a trained classifier: there's no data yet to train one on (P2). They're the same for every team (P3).
A route never waits for an answer to judge it, so streaming and latency are unchanged (P1).
"""
from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Rules:
    """Any one of these sends a request to the upgrade model. Starting guesses, not measured (P4)."""

    has_tools: bool = True  # calling tools well is where small models fail most
    min_turns: Optional[int] = 6  # user messages in the conversation: long sessions are usually harder ones
    min_prompt_tokens: Optional[int] = 3000  # long inputs: more to keep track of


@dataclass(frozen=True)
class Route:
    name: str  # what clients ask for, e.g. "auto"
    default: str  # a configured model name
    upgrade_to: str  # a configured model name
    rules: Rules


def choose(route: Route, payload: dict, prompt_tokens: int) -> Tuple[str, str]:
    """(model name, reason). The reason is a fixed word, safe as a metric label."""
    rules = route.rules
    if rules.has_tools and (payload.get("tools") or payload.get("functions")):
        return route.upgrade_to, "tools"
    turns = sum(1 for m in payload.get("messages", []) if m.get("role") == "user")
    if rules.min_turns is not None and turns >= rules.min_turns:
        return route.upgrade_to, "turns"
    if rules.min_prompt_tokens is not None and prompt_tokens >= rules.min_prompt_tokens:
        return route.upgrade_to, "long_prompt"
    return route.default, "default"
