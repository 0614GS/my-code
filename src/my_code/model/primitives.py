"""Model boundary primitives shared by requests and persisted conversation facts."""

import re
from dataclasses import dataclass
from typing import Literal

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]
type ReasoningDisclosure = Literal["verbatim", "summary", "redacted", "hidden"]
type ReplayScope = Literal["active_trajectory", "working_context"]

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def to_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [to_json_value(item) for item in value]
    if isinstance(value, dict):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            result[key] = to_json_value(item)
        return result
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def to_json_object(value: object) -> JsonObject:
    converted = to_json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("Expected a JSON object")
    return converted


def validate_provider_id(value: str) -> str:
    if _PROVIDER_ID.fullmatch(value) is None:
        raise ValueError("provider ID must match [a-z0-9][a-z0-9_-]{0,63}")
    return value


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Token usage reported for one model request."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    provider_reported: bool = False

    def __post_init__(self) -> None:
        values = (
            self.input_tokens,
            self.output_tokens,
            self.cache_creation_input_tokens,
            self.cache_read_input_tokens,
        )
        if any(value < 0 for value in values):
            raise ValueError("Token usage must not be negative")

    @property
    def total_input_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass(frozen=True, slots=True)
class ReasoningPresentation:
    """Provider-neutral reasoning content safe for conversation and UI projection."""

    disclosure: ReasoningDisclosure
    parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not all(isinstance(part, str) and bool(part) for part in self.parts):
            raise ValueError("Reasoning presentation parts must be non-empty strings")
        if self.disclosure in {"verbatim", "summary"} and not self.parts:
            raise ValueError("Visible reasoning must contain presentation parts")
        if self.disclosure in {"redacted", "hidden"} and self.parts:
            raise ValueError("Hidden reasoning must not contain presentation parts")


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """Binding that prevents private continuation data crossing model connections."""

    protocol: str
    provider_id: str
    model: str
    base_url: str | None = None

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and bool(value.strip())
            for value in (self.protocol, self.provider_id, self.model)
        ):
            raise ValueError("Provider binding strings must not be empty")
        try:
            validate_provider_id(self.provider_id)
        except ValueError as error:
            raise ValueError("Provider binding provider_id is invalid") from error
        if self.base_url is not None and not self.base_url.strip():
            raise ValueError("Provider binding base_url must be non-empty or null")


@dataclass(frozen=True, slots=True)
class ProviderContinuationState:
    """Opaque provider payload replayed only through a matching binding."""

    binding: ProviderBinding
    replay_scope: ReplayScope
    payload: JsonObject

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", to_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class ProviderReplayRecord:
    """Opaque provider replay data linked to canonical entry/content positions."""

    entry_id: str
    content_id: str
    state: ProviderContinuationState

    def __post_init__(self) -> None:
        if not self.entry_id.strip() or not self.content_id.strip():
            raise ValueError("Provider replay entry and content IDs must not be empty")
        if not isinstance(self.state, ProviderContinuationState):
            raise TypeError("Provider replay state is required")


def replay_content_id(index: int) -> str:
    if index < 0:
        raise ValueError("Replay content index must not be negative")
    return f"content:{index}"


__all__ = [
    "JsonObject",
    "JsonValue",
    "ProviderBinding",
    "ProviderContinuationState",
    "ProviderReplayRecord",
    "replay_content_id",
    "ReasoningDisclosure",
    "ReasoningPresentation",
    "ReplayScope",
    "TokenUsage",
    "to_json_object",
    "to_json_value",
    "validate_provider_id",
]
