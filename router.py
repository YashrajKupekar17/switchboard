"""Model routing: a client asks for a route ("auto") instead of a model, and the gateway picks one. Decisions: DECISIONS.md P.

    request for "auto" -> look at the request only (no model is called) -> the route's default model,
                          or its upgrade model if any rule says the request looks hard

Rules, not a trained classifier: there's no data yet to train one on (P2). They're the same for every team (P3).
A route never waits for an answer to judge it, so streaming and latency are unchanged (P1).

Shadow mode (P5): for a sample of "auto" requests, after the answer is sent, the gateway also asks the route's other
model and saves both answers. scripts/route_eval.py has a judge compare them later, per team and per rule.
"""
import json
import logging
import random
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import redis.asyncio as redis
from redis.exceptions import RedisError

log = logging.getLogger("gateway")

SHADOW_MAX_TOKENS = 4000  # bigger prompts aren't sampled: the record keeps the whole conversation, in a cache that evicts
SHADOW_KEEP = 1000  # records kept per team
SHADOW_TTL = 7 * 86400


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
    shadow_sample: float = 0.0  # share of requests that also get the other model's answer, for route_eval.py

    def other(self, model: str) -> str:
        return self.default if model == self.upgrade_to else self.upgrade_to


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


def should_shadow(route: Route, prompt_tokens: int, rand: Callable[[], float] = random.random) -> bool:
    return route.shadow_sample > 0 and prompt_tokens <= SHADOW_MAX_TOKENS and rand() < route.shadow_sample


def answer_of(body: dict) -> dict:
    """The part of a completion a judge compares: the text, and any tool calls."""
    message = ((body.get("choices") or [{}])[0].get("message")) or {}
    return {k: message[k] for k in ("content", "tool_calls") if message.get(k)}


def shadow_record(route: Route, team: str, reason: str, served: str, payload: dict, served_body: dict,
                  other_body: dict, at: str) -> dict:
    strong, cheap = (served_body, other_body) if served == route.upgrade_to else (other_body, served_body)
    return {
        "at": at, "team": team, "route": route.name, "reason": reason, "served": served,
        "strong_model": route.upgrade_to, "cheap_model": route.default, "messages": payload["messages"],
        "strong_answer": answer_of(strong), "cheap_answer": answer_of(cheap),
        "verdict": None,  # filled in by route_eval.py: cheap_as_good, strong_better or unsure
    }


class ShadowLog:
    """Shadow records in redis-cache, a capped list per team. Losing them only loses measurements (fail open)."""

    def __init__(self, client: redis.Redis):
        self.redis = client

    async def save(self, team: str, record: dict) -> bool:
        key = f"route_shadow:{team}"
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.lpush(key, json.dumps(record, ensure_ascii=False))
                pipe.ltrim(key, 0, SHADOW_KEEP - 1)
                pipe.expire(key, SHADOW_TTL)
                await pipe.execute()
            return True
        except (RedisError, OSError) as exc:
            log.warning("route shadow: couldn't save a record (%r)", exc)
            return False

    async def records(self, team: str) -> List[dict]:
        return [json.loads(r) for r in await self.redis.lrange(f"route_shadow:{team}", 0, -1)]


# --- the offline judge (scripts/route_eval.py), here so it's tested with the rest ---

JUDGE_MODEL = "gpt-4o"  # offline and on a sample, so the stronger model; it shares a family with one side (P5)
JUDGE_PROMPT = ("Two assistants answered the same conversation.\n\n"
                "Conversation:\n{conversation}\n\n"
                "Answer 1:\n{first}\n\n"
                "Answer 2:\n{second}\n\n"
                "Which answer is better for the user: correct, complete and helpful? If they are about equally good, "
                "say tie. Reply with only 1, 2 or tie.")
MAX_MESSAGE_CHARS = 3000


def render_conversation(messages: List[dict]) -> str:
    return "\n".join(f"{m.get('role')}: {str(m.get('content') or '')[:MAX_MESSAGE_CHARS]}" for m in messages)


def render_answer(answer: dict) -> str:
    parts = [answer.get("content") or ""]
    if answer.get("tool_calls"):
        parts.append("tool calls: " + json.dumps(answer["tool_calls"], ensure_ascii=False))
    return "\n".join(p for p in parts if p)


def verdict(strong_first: str, cheap_first: str) -> str:
    """Both orders' answers ("1", "2" or "tie") -> cheap_as_good, strong_better or unsure.

    Asked twice with the answers swapped, because judges favour whichever comes first (position bias).
    Only a verdict both orders agree on counts."""
    cheap_ok = strong_first in ("2", "tie") and cheap_first in ("1", "tie")
    strong_won = strong_first == "1" and cheap_first == "2"
    return "cheap_as_good" if cheap_ok else "strong_better" if strong_won else "unsure"
