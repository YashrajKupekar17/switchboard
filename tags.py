"""Request tags for cost attribution: `x-gateway-tags: feature=search,env=prod` (DECISIONS.md T1).

A team's total says how much it spent; tags say on what. They're stored with each request in the usage ledger
and reported by scripts/spend.py. They're never metric labels: every new value would be a new Prometheus time
series, and clients choose the values.

Malformed tags are refused with a 400 rather than dropped. Attribution that silently loses requests is worse
than none, because nobody notices until the report is wrong.
"""
import re
from typing import Dict, Optional

MAX_TAGS = 10
NAME = re.compile(r"[a-z][a-z0-9_.-]{0,31}")  # lowercase, so "Env" and "env" can't become two columns in a report
VALUE = re.compile(r"[A-Za-z0-9_.:/@-]{1,64}")


class BadTags(ValueError):
    pass


def parse(header: Optional[str]) -> Dict[str, str]:
    if not header or not header.strip():
        return {}
    tags = {}
    for part in header.split(","):
        name, sep, value = part.strip().partition("=")
        if not sep or not NAME.fullmatch(name) or not VALUE.fullmatch(value):
            raise BadTags(f"x-gateway-tags: {part.strip()!r} isn't name=value (name: lowercase letters, digits, "
                          f"_ . -, up to 32; value: letters, digits, _ . : / @ -, up to 64)")
        if name in tags:
            raise BadTags(f"x-gateway-tags: {name!r} appears twice")
        tags[name] = value
    if len(tags) > MAX_TAGS:
        raise BadTags(f"x-gateway-tags: at most {MAX_TAGS} tags, got {len(tags)}")
    return tags
