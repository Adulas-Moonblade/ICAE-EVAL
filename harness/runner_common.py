"""Result and error contract shared by the coding-agent runners."""
from dataclasses import dataclass


REFUSAL_MARKERS = [
    "Usage Policy", "usage policies", "I can't help with that",
    "I cannot help with", "I'm not able to help", "against Anthropic",
]
RATELIMIT_MARKERS = [
    "429", "每分钟请求次数超过限制", "rate limit", "rate_limit",
    "overloaded", "Overloaded",
]


class RateLimited(Exception):
    pass


@dataclass
class AgentResult:
    status: str           # "success" | "refused" | "error"
    is_error: bool = False
    refused: bool = False
    cost_usd: float = 0.0
    num_turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    tail: str = ""
    raw_subtype: str = ""
    detail: str = ""


def _contains(haystack: str, markers) -> bool:
    h = haystack or ""
    return any(m in h for m in markers)
