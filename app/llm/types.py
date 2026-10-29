"""Gateway data types (design doc 7.1)."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Role(StrEnum):
    INTAKE = "intake"
    VISION = "vision"
    VOICE = "voice"
    EXPLAIN = "explain"
    NARRATE = "narrate"
    MESSAGE = "message"
    AGENT = "agent"  # roster agent (7.10)


@dataclass
class LLMRequest:
    role: Role
    messages: list[dict[str, Any]]  # OpenAI chat format; content may include image/audio parts
    tools: list[dict[str, Any]] | None = None  # OpenAI tool specs
    response_schema: dict[str, Any] | None = None  # JSON schema for structured output
    temperature: float = 0.0
    max_tokens: int = 4000  # Muse Spark Open reasons first: room for reasoning plus the answer
    timeout_s: float | None = None  # per-call HTTP timeout (photo reads); not part of the cache key


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    text: str | None
    tool_calls: list[ToolCall]
    parsed: dict[str, Any] | None  # when response_schema was given and parsing succeeded
    reasoning: str | None  # stored for the Log tab, never shown to users or checked
    provider: str
    model: str
    cache_hit: bool
    latency_ms: int
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float
    reasoning_tokens: int = 0
    call_id: str = ""
    json_protocol: bool = False  # tools were run through the JSON protocol fallback


@dataclass(frozen=True)
class ProviderCaps:
    native_tools: bool = True
    json_schema: bool = True
    vision: bool = False
    audio: bool = False


@dataclass
class ProviderResult:
    """What a provider returns before the gateway adds cache, cost and logging."""

    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    reasoning: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_field: str | None = None  # which response field carried the reasoning (smoke)


class LLMError(Exception):
    code = "LLM_ERROR"


class SpendCapReached(LLMError):
    code = "LLM_SPEND_CAP"


class CacheMiss(LLMError):
    code = "LLM_CACHE_MISS"


class ProviderError(LLMError):
    code = "LLM_PROVIDER_ERROR"


class MalformedToolCall(ProviderError):
    code = "LLM_MALFORMED_TOOL_CALL"
