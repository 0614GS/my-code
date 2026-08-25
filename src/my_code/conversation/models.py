"""运行时会话消息；展示内容与 provider 续接状态严格分离。"""

from dataclasses import dataclass, field
from typing import Literal

from my_code.conversation.attachments import AttachmentPayload
from my_code.conversation.presentation import ToolResultPresentation
from my_code.conversation.primitives import new_id, utc_now
from my_code.model.primitives import (
    JsonObject,
    ProviderBinding,
    ReasoningPresentation,
    TokenUsage,
    to_json_object,
)


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
        object.__setattr__(self, "input", to_json_object(self.input))


@dataclass(frozen=True, slots=True)
class ReasoningContent:
    id: str
    presentation: ReasoningPresentation
    kind: Literal["reasoning"] = field(default="reasoning", init=False)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Reasoning id must not be empty")


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_use_id: str
    content: str
    presentation: ToolResultPresentation
    is_error: bool = False
    kind: Literal["tool_result"] = field(default="tool_result", init=False)

    def __post_init__(self) -> None:
        if not self.tool_use_id:
            raise ValueError("Tool result id must not be empty")
        if not isinstance(self.presentation, ToolResultPresentation):
            raise TypeError("Tool results require a presentation")


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
    content: tuple["AssistantContent", ...]
    usage: TokenUsage
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    provider_binding: ProviderBinding | None = None
    request_input_tokens_estimate: int | None = None
    kind: Literal["assistant"] = field(default="assistant", init=False)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Assistant message content must not be empty")
        if not all(
            isinstance(block, (TextContent, ToolCall, ReasoningContent))
            for block in self.content
        ):
            raise TypeError(
                "Assistant messages may contain only text and tool calls or reasoning"
            )
        if not any(
            isinstance(block, ToolCall)
            or isinstance(block, TextContent)
            and bool(block.text)
            for block in self.content
        ):
            raise ValueError("Assistant message contained no actionable content")
        if not isinstance(self.usage, TokenUsage):
            raise TypeError("Assistant messages require token usage")
        if (
            self.request_input_tokens_estimate is not None
            and self.request_input_tokens_estimate < 1
        ):
            raise ValueError("Assistant request token estimate must be positive")
        call_ids = [block.id for block in self.content if isinstance(block, ToolCall)]
        if len(call_ids) != len(set(call_ids)):
            raise ValueError("Assistant message contains duplicate tool call IDs")

    @property
    def starts_human_turn(self) -> bool:
        return False

    @property
    def starts_context_segment(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class ToolResultBatch:
    content: tuple[ToolResult, ...]
    source_assistant_id: str
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    kind: Literal["tool_result_batch"] = field(default="tool_result_batch", init=False)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Tool result batch content must not be empty")
        if not all(isinstance(result, ToolResult) for result in self.content):
            raise TypeError("Tool result batches may contain only tool results")
        if not self.source_assistant_id:
            raise ValueError("Tool result source assistant ID must not be empty")
        result_ids = [result.tool_use_id for result in self.content]
        if len(result_ids) != len(set(result_ids)):
            raise ValueError("Tool result batch contains duplicate result IDs")

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


@dataclass(frozen=True, slots=True)
class AttachmentMessage:
    """Structured auxiliary context at an exact conversation position."""

    payload: AttachmentPayload
    uuid: str = field(default_factory=new_id)
    parent_uuid: str | None = None
    timestamp: str = field(default_factory=utc_now)
    kind: Literal["attachment"] = field(default="attachment", init=False)

    @property
    def starts_human_turn(self) -> bool:
        return False

    @property
    def starts_context_segment(self) -> bool:
        return False


type ConversationEntry = (
    HumanMessage
    | AssistantMessage
    | ToolResultBatch
    | ConversationSummaryMessage
    | AttachmentMessage
)
type AssistantContent = TextContent | ToolCall | ReasoningContent

__all__ = [
    "AssistantContent",
    "AssistantMessage",
    "AttachmentMessage",
    "ConversationEntry",
    "ConversationSummaryMessage",
    "HumanMessage",
    "ReasoningContent",
    "TextContent",
    "ToolCall",
    "ToolResult",
    "ToolResultBatch",
]
