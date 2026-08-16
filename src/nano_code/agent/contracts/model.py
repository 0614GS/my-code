"""完整、provider 无关的模型请求与输出协议。"""

from dataclasses import dataclass, field
from typing import Literal

from nano_code.messages.primitives import JsonObject, TokenUsage
from nano_code.prompts import SystemPrompt


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    """一次模型请求可见的工具名称、说明和输入 schema。"""

    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class ModelTextBlock:
    text: str
    type: Literal["text"] = field(default="text", init=False)


@dataclass(frozen=True, slots=True)
class ModelToolUseBlock:
    id: str
    name: str
    input: JsonObject
    type: Literal["tool_use"] = field(default="tool_use", init=False)


@dataclass(frozen=True, slots=True)
class ModelToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    type: Literal["tool_result"] = field(default="tool_result", init=False)


type ModelUserContent = ModelTextBlock | ModelToolResultBlock
type ModelAssistantContent = ModelTextBlock | ModelToolUseBlock


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
            isinstance(block, (ModelTextBlock, ModelToolUseBlock))
            for block in self.content
        ):
            raise TypeError("Model assistant messages contain only text or tool uses")


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
            isinstance(block, (ModelTextBlock, ModelToolUseBlock))
            for block in self.content
        ):
            raise TypeError("Model output contains only assistant content")


@dataclass(frozen=True, slots=True)
class ModelTextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class ModelOutputCompleted:
    output: ModelOutput


type ModelStreamEvent = ModelTextDelta | ModelOutputCompleted

__all__ = [
    "ModelAssistantContent",
    "ModelAssistantMessage",
    "ModelMessage",
    "ModelOutput",
    "ModelOutputCompleted",
    "ModelRequest",
    "ModelStreamEvent",
    "ModelTextBlock",
    "ModelTextDelta",
    "ModelToolResultBlock",
    "ModelToolDefinition",
    "ModelToolUseBlock",
    "ModelUserContent",
    "ModelUserMessage",
]
