"""Primitive values carried by conversation and tool content."""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """一次模型请求的 token 用量。"""

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


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid4())


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


__all__ = [
    "JsonObject",
    "JsonValue",
    "TokenUsage",
    "new_id",
    "to_json_object",
    "to_json_value",
    "utc_now",
]
