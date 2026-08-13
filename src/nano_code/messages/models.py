"""Provider-neutral message types used by the agent core."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

# Restrict cross-layer data to JSON rather than allowing Any. Provider payloads,
# transcripts, and tool inputs can therefore be checked at every boundary.
type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]
type MessageRole = Literal["user", "assistant"]
type MessageOrigin = Literal["human", "model", "tool", "system"]


def utc_now() -> str:
    """Return a compact, timezone-aware timestamp."""

    return datetime.now(UTC).isoformat()


def new_id() -> str:
    """Return a stable local message identifier."""

    return str(uuid4())


def to_json_value(value: object) -> JsonValue:
    """Validate and copy an arbitrary value into the supported JSON domain."""

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
    """Validate an arbitrary value as a JSON object."""

    converted = to_json_value(value)
    if not isinstance(converted, dict):
        raise TypeError("Expected a JSON object")
    return converted


@dataclass(frozen=True, slots=True)
class TextBlock:
    """A user or assistant text block."""

    text: str
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    """A model request to invoke a named tool."""

    id: str
    name: str
    input: JsonObject
    # Literal discriminators give mypy the same narrowing role that tagged
    # unions provide in the TypeScript reference implementation.
    type: Literal["tool_use"] = field(default="tool_use", init=False)


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    """The result paired with one model tool request."""

    # tool_use_id is provider protocol identity; it is intentionally separate
    # from the local transcript message UUID below.
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = field(default="tool_result", init=False)


type ContentBlock = TextBlock | ToolUseBlock | ToolResultBlock
type AssistantBlock = TextBlock | ToolUseBlock


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """One persisted message in the internal transcript."""

    # role is the provider protocol role. origin records who actually created
    # the message, because tool results also travel under the provider's user role.
    role: MessageRole
    content: tuple[ContentBlock, ...]
    origin: MessageOrigin

    # Local UUIDs form the recoverable transcript chain independently of API IDs.
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    # Tool-result messages retain a direct provenance edge to the assistant
    # message that requested them; this becomes important for parallel calls.
    source_message_uuid: str | None = None

    def __post_init__(self) -> None:
        # Enforce provider-shape invariants at construction time so malformed
        # messages cannot reach persistence and fail much later during sampling.
        if not self.content:
            raise ValueError("A message must contain at least one content block")
        if self.role == "assistant" and any(
            isinstance(block, ToolResultBlock) for block in self.content
        ):
            raise ValueError("Assistant messages cannot contain tool results")
        if self.role == "user" and any(
            isinstance(block, ToolUseBlock) for block in self.content
        ):
            raise ValueError("User messages cannot contain tool uses")
        if self.origin == "tool" and not all(
            isinstance(block, ToolResultBlock) for block in self.content
        ):
            raise ValueError("Tool-origin messages may contain only tool results")

    @property
    def starts_human_turn(self) -> bool:
        """Whether this is a real user prompt and therefore a safe cut boundary."""

        return self.role == "user" and self.origin == "human"


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """Provider token accounting for one model request."""

    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """A provider-neutral assistant response."""

    content: tuple[AssistantBlock, ...]
    stop_reason: str
    usage: TokenUsage = field(default_factory=TokenUsage)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Model response contained no supported content blocks")
