"""运行时会话消息；展示内容与 provider 续接状态严格分离。"""

import re
from dataclasses import dataclass, field
from typing import Literal

from nano_code.application.chat.presentation import ToolResultPresentation
from nano_code.conversation.primitives import (
    JsonObject,
    TokenUsage,
    new_id,
    to_json_object,
    utc_now,
)

type ReasoningDisclosure = Literal["verbatim", "summary", "redacted", "hidden"]
type ReplayScope = Literal["active_trajectory", "working_context"]

_PROVIDER_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ReasoningPresentation:
    """唯一可越过 application/UI 边界的 reasoning 内容。"""

    disclosure: ReasoningDisclosure
    parts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.disclosure not in {"verbatim", "summary", "redacted", "hidden"}:
            raise ValueError("Unsupported reasoning disclosure")
        if not all(isinstance(part, str) and bool(part) for part in self.parts):
            raise ValueError("Reasoning presentation parts must be non-empty strings")
        if self.disclosure in {"verbatim", "summary"} and not self.parts:
            raise ValueError("Visible reasoning must contain presentation parts")
        if self.disclosure in {"redacted", "hidden"} and self.parts:
            raise ValueError("Hidden reasoning must not contain presentation parts")


@dataclass(frozen=True, slots=True)
class ProviderBinding:
    """阻止私有续接数据跨 profile、模型或 endpoint 回放。"""

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
        if _PROVIDER_ID.fullmatch(self.provider_id) is None:
            raise ValueError("Provider binding provider_id is invalid")
        if self.base_url is not None and not self.base_url.strip():
            raise ValueError("Provider binding base_url must be non-empty or null")


@dataclass(frozen=True, slots=True)
class ProviderContinuationState:
    """不透明、只供匹配 adapter 回放的 provider 原始状态。"""

    binding: ProviderBinding
    replay_scope: ReplayScope
    payload: JsonObject

    def __post_init__(self) -> None:
        if self.replay_scope not in {"active_trajectory", "working_context"}:
            raise ValueError("Unsupported provider continuation replay scope")
        object.__setattr__(self, "payload", to_json_object(self.payload))


@dataclass(frozen=True, slots=True)
class TextContent:
    text: str
    continuation: ProviderContinuationState | None = None
    kind: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    input: JsonObject
    continuation: ProviderContinuationState | None = None
    kind: Literal["tool_call"] = field(default="tool_call", init=False)

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("Tool call id and name must not be empty")
        object.__setattr__(self, "input", to_json_object(self.input))


@dataclass(frozen=True, slots=True)
class ReasoningContent:
    id: str
    presentation: ReasoningPresentation
    continuation: ProviderContinuationState | None = None
    kind: Literal["reasoning"] = field(default="reasoning", init=False)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Reasoning id must not be empty")


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
type AssistantContent = TextContent | ToolCall | ReasoningContent

__all__ = [
    "AssistantContent",
    "AssistantMessage",
    "ConversationMessage",
    "ConversationSummaryMessage",
    "HumanMessage",
    "ProviderBinding",
    "ProviderContinuationState",
    "ReasoningContent",
    "ReasoningDisclosure",
    "ReasoningPresentation",
    "ReplayScope",
    "TextContent",
    "ToolCall",
    "ToolResult",
    "ToolResultsMessage",
]
