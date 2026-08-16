"""运行时会话消息；其判别类型不暴露模型 API role。"""

from dataclasses import dataclass, field
from typing import Literal

from nano_code.messages.primitives import JsonObject, TokenUsage, new_id, utc_now
from nano_code.presentation import ToolResultPresentation


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str
    kind: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    input: JsonObject
    kind: Literal["tool_call"] = field(default="tool_call", init=False)

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("Tool call id and name must not be empty")


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False
    presentation: ToolResultPresentation | None = None
    kind: Literal["tool_result"] = field(default="tool_result", init=False)

    def __post_init__(self) -> None:
        if not self.tool_use_id:
            raise ValueError("Tool result id must not be empty")


@dataclass(frozen=True, slots=True)
class HumanMessage:
    content: str
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    kind: Literal["human"] = field(default="human", init=False)

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Human message content must not be empty")

    @property
    def starts_human_turn(self) -> bool:
        return True

    @property
    def starts_context_segment(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class AssistantMessage:
    content: tuple[TextContent | ToolCall, ...]
    usage: TokenUsage
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    kind: Literal["assistant"] = field(default="assistant", init=False)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Assistant message content must not be empty")
        if not all(
            isinstance(block, (TextContent, ToolCall)) for block in self.content
        ):
            raise TypeError("Assistant messages may contain only text and tool calls")
        if not isinstance(self.usage, TokenUsage):
            raise TypeError("Assistant messages require token usage")

    @property
    def starts_human_turn(self) -> bool:
        return False

    @property
    def starts_context_segment(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ToolResultsMessage:
    content: tuple[ToolResult, ...]
    source_assistant_uuid: str
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    kind: Literal["tool_results"] = field(default="tool_results", init=False)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Tool results message content must not be empty")
        if not all(isinstance(result, ToolResult) for result in self.content):
            raise TypeError("Tool results messages may contain only tool results")
        if not self.source_assistant_uuid:
            raise ValueError("Tool results source assistant UUID must not be empty")

    @property
    def starts_human_turn(self) -> bool:
        return False

    @property
    def starts_context_segment(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ConversationSummaryMessage:
    content: str
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    kind: Literal["conversation_summary"] = field(
        default="conversation_summary", init=False
    )

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("Conversation summary must not be empty")

    @property
    def starts_human_turn(self) -> bool:
        return False

    @property
    def starts_context_segment(self) -> bool:
        return True


type ConversationMessage = (
    HumanMessage | AssistantMessage | ToolResultsMessage | ConversationSummaryMessage
)
type AssistantContent = TextContent | ToolCall

__all__ = [
    "AssistantContent",
    "AssistantMessage",
    "ConversationMessage",
    "ConversationSummaryMessage",
    "HumanMessage",
    "TextContent",
    "ToolCall",
    "ToolResult",
    "ToolResultsMessage",
]
