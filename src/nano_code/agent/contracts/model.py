"""完整、provider 无关的模型请求、reasoning 展示与续接协议。"""

from dataclasses import dataclass, field
from typing import Literal

from nano_code.conversation import (
    JsonObject,
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
    to_json_object,
)
from nano_code.prompts import SystemPrompt


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ModelTextBlock:
    text: str
    continuation: ProviderContinuationState | None = None
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class ModelToolUseBlock:
    id: str
    name: str
    input: JsonObject
    continuation: ProviderContinuationState | None = None
    type: Literal["tool_use"] = field(default="tool_use", init=False)

    def __post_init__(self) -> None:
        if not self.id or not self.name:
            raise ValueError("Model tool use id and name must not be empty")
        object.__setattr__(self, "input", to_json_object(self.input))


@dataclass(frozen=True, slots=True)
class ModelToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = field(default="tool_result", init=False)


@dataclass(frozen=True, slots=True)
class ModelReasoningBlock:
    id: str
    presentation: ReasoningPresentation
    continuation: ProviderContinuationState | None = None
    type: Literal["reasoning"] = field(default="reasoning", init=False)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Model reasoning id must not be empty")


type ModelUserContent = ModelTextBlock | ModelToolResultBlock
type ModelAssistantContent = ModelTextBlock | ModelToolUseBlock | ModelReasoningBlock


@dataclass(frozen=True, slots=True)
class ModelUserMessage:
    content: tuple[ModelUserContent, ...]
    role: Literal["user"] = field(default="user", init=False)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Model user message content must not be empty")
        if not all(
            isinstance(block, (ModelTextBlock, ModelToolResultBlock))
            for block in self.content
        ):
            raise TypeError("Model user messages contain only text or tool results")


@dataclass(frozen=True, slots=True)
class ModelAssistantMessage:
    content: tuple[ModelAssistantContent, ...]
    role: Literal["assistant"] = field(default="assistant", init=False)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Model assistant message content must not be empty")
        if not all(
            isinstance(block, (ModelTextBlock, ModelToolUseBlock, ModelReasoningBlock))
            for block in self.content
        ):
            raise TypeError(
                "Model assistant messages contain only text or tool uses or reasoning"
            )
        if not any(
            isinstance(block, ModelToolUseBlock)
            or isinstance(block, ModelTextBlock)
            and bool(block.text)
            for block in self.content
        ):
            raise ValueError("Model assistant message contained no actionable content")


type ModelMessage = ModelUserMessage | ModelAssistantMessage


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_prompt: SystemPrompt
    messages: tuple[ModelMessage, ...]
    tools: tuple[ModelToolDefinition, ...]
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")


@dataclass(frozen=True, slots=True)
class ModelOutput:
    content: tuple[ModelAssistantContent, ...]
    stop_reason: str
    usage: TokenUsage = field(default_factory=TokenUsage)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Model output contained no supported content blocks")
        if not all(
            isinstance(block, (ModelTextBlock, ModelToolUseBlock, ModelReasoningBlock))
            for block in self.content
        ):
            raise TypeError("Model output contains only assistant content")
        if not any(
            isinstance(block, ModelToolUseBlock)
            or isinstance(block, ModelTextBlock)
            and bool(block.text)
            for block in self.content
        ):
            raise ValueError("Model output contained no actionable content blocks")


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelReasoningStarted:
    id: str
    disclosure: Literal["verbatim", "summary", "redacted", "hidden"]


@dataclass(frozen=True, slots=True)
class ModelReasoningDelta:
    id: str
    disclosure: Literal["verbatim", "summary", "redacted", "hidden"]
    part_index: int
    text: str


@dataclass(frozen=True, slots=True)
class ModelReasoningCompleted:
    id: str


@dataclass(frozen=True, slots=True)
class ModelOutputCompleted:
    output: ModelOutput


type ModelStreamEvent = (
    ModelTextDelta
    | ModelReasoningStarted
    | ModelReasoningDelta
    | ModelReasoningCompleted
    | ModelOutputCompleted
)

__all__ = [name for name in globals() if name.startswith("Model")]
