"""Provider-neutral model requests, ordered input items, and outputs."""

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from my_code.foundation.json import JsonObject, to_json_object
from my_code.model.primitives import (
    ProviderContinuationState,
    ReasoningPresentation,
    TokenUsage,
)


class PromptStability(StrEnum):
    STATIC = "static"
    SESSION = "session"
    REQUEST = "request"


@dataclass(frozen=True, slots=True)
class ResolvedPromptSection:
    key: str
    content: str
    stability: PromptStability

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("Resolved prompt section key must not be empty")
        if not self.content.strip():
            raise ValueError(f"Prompt section {self.key!r} resolved to empty content")


@dataclass(frozen=True, slots=True)
class SystemPrompt:
    """A resolved system prompt whose section boundaries remain available."""

    sections: tuple[ResolvedPromptSection, ...]

    def __post_init__(self) -> None:
        if not self.sections:
            raise ValueError("System prompt must contain at least one section")

    @property
    def text(self) -> str:
        return "\n\n".join(section.content for section in self.sections)

    @classmethod
    def from_text(
        cls,
        content: str,
        *,
        key: str = "request",
        stability: PromptStability = PromptStability.REQUEST,
    ) -> "SystemPrompt":
        return cls((ResolvedPromptSection(key, content, stability),))


@dataclass(frozen=True, slots=True)
class ModelToolDefinition:
    name: str
    description: str
    input_schema: JsonObject


@dataclass(frozen=True, slots=True)
class InputText:
    text: str
    type: Literal["input_text"] = field(default="input_text", init=False)

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("Input text must not be empty")


@dataclass(frozen=True, slots=True)
class InputImage:
    media_type: str
    data: str
    type: Literal["input_image"] = field(default="input_image", init=False)

    def __post_init__(self) -> None:
        if not self.media_type.startswith("image/") or not self.data:
            raise ValueError("Input image requires an image media type and data")


@dataclass(frozen=True, slots=True)
class InputDocument:
    media_type: str
    data: str
    name: str | None = None
    type: Literal["input_document"] = field(default="input_document", init=False)

    def __post_init__(self) -> None:
        if not self.media_type.strip() or not self.data:
            raise ValueError("Input document requires a media type and data")
        if self.name is not None and not self.name.strip():
            raise ValueError("Input document name must not be empty")


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
class ToolOutputText:
    text: str
    type: Literal["output_text"] = field(default="output_text", init=False)

    def __post_init__(self) -> None:
        if not self.text:
            raise ValueError("Tool output text must not be empty")


@dataclass(frozen=True, slots=True)
class ToolOutputImage:
    media_type: str
    data: str
    type: Literal["output_image"] = field(default="output_image", init=False)

    def __post_init__(self) -> None:
        if not self.media_type.startswith("image/") or not self.data:
            raise ValueError("Tool output image requires an image media type and data")


@dataclass(frozen=True, slots=True)
class ToolOutputDocument:
    media_type: str
    data: str
    name: str | None = None
    type: Literal["output_document"] = field(default="output_document", init=False)

    def __post_init__(self) -> None:
        if not self.media_type.strip() or not self.data:
            raise ValueError("Tool output document requires a media type and data")
        if self.name is not None and not self.name.strip():
            raise ValueError("Tool output document name must not be empty")


@dataclass(frozen=True, slots=True)
class ModelReasoningBlock:
    id: str
    presentation: ReasoningPresentation
    continuation: ProviderContinuationState | None = None
    type: Literal["reasoning"] = field(default="reasoning", init=False)

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Model reasoning id must not be empty")


type ModelAssistantContent = ModelTextBlock | ModelToolUseBlock | ModelReasoningBlock
type InputContent = InputText | InputImage | InputDocument
type ToolOutputContent = ToolOutputText | ToolOutputImage | ToolOutputDocument


@dataclass(frozen=True, slots=True)
class UserInput:
    content: tuple[InputContent, ...]
    type: Literal["user_input"] = field(default="user_input", init=False)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("User input content must not be empty")
        if not all(
            isinstance(block, (InputText, InputImage, InputDocument))
            for block in self.content
        ):
            raise TypeError("User input contains only input content")


@dataclass(frozen=True, slots=True)
class AssistantOutput:
    content: tuple[ModelAssistantContent, ...]
    type: Literal["assistant_output"] = field(default="assistant_output", init=False)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("Assistant output content must not be empty")
        if not all(
            isinstance(block, (ModelTextBlock, ModelToolUseBlock, ModelReasoningBlock))
            for block in self.content
        ):
            raise TypeError(
                "Assistant output contains only text, tool calls, or reasoning"
            )
        if not any(
            isinstance(block, ModelToolUseBlock)
            or isinstance(block, ModelTextBlock)
            and bool(block.text)
            for block in self.content
        ):
            raise ValueError("Assistant output contained no actionable content")


@dataclass(frozen=True, slots=True)
class ToolOutput:
    call_id: str
    content: tuple[ToolOutputContent, ...]
    is_error: bool = False
    type: Literal["tool_output"] = field(default="tool_output", init=False)

    def __post_init__(self) -> None:
        if not self.call_id:
            raise ValueError("Tool output call id must not be empty")
        if not self.content:
            raise ValueError("Tool output content must not be empty")
        if not all(
            isinstance(block, (ToolOutputText, ToolOutputImage, ToolOutputDocument))
            for block in self.content
        ):
            raise TypeError("Tool output contains only tool output content")


@dataclass(frozen=True, slots=True)
class ToolOutputs:
    results: tuple[ToolOutput, ...]
    type: Literal["tool_outputs"] = field(default="tool_outputs", init=False)

    def __post_init__(self) -> None:
        if not self.results:
            raise ValueError("Tool outputs must contain at least one result")
        if not all(isinstance(result, ToolOutput) for result in self.results):
            raise TypeError("Tool outputs contain only ToolOutput values")


type ModelInputItem = UserInput | AssistantOutput | ToolOutputs


@dataclass(frozen=True, slots=True)
class ModelRequest:
    system_prompt: SystemPrompt
    input: tuple[ModelInputItem, ...]
    tools: tuple[ModelToolDefinition, ...]
    max_output_tokens: int

    def __post_init__(self) -> None:
        if self.max_output_tokens < 1:
            raise ValueError("max_output_tokens must be positive")
        validate_model_input(self.input)


def validate_model_input(items: tuple[ModelInputItem, ...]) -> None:
    """Validate tool protocol over semantic items without provider roles."""

    pending: set[str] = set()
    seen_calls: set[str] = set()
    seen_outputs: set[str] = set()
    for item in items:
        if pending and not isinstance(item, ToolOutputs):
            raise ValueError(
                "Unresolved tool use before next model input item: "
                f"{', '.join(sorted(pending))}"
            )
        if isinstance(item, AssistantOutput):
            for block in item.content:
                if not isinstance(block, ModelToolUseBlock):
                    continue
                if block.id in seen_calls:
                    raise ValueError(f"Duplicate tool use in model input: {block.id}")
                seen_calls.add(block.id)
                pending.add(block.id)
        elif isinstance(item, ToolOutputs):
            for result in item.results:
                if result.call_id in seen_outputs:
                    raise ValueError(
                        f"Duplicate tool output in model input: {result.call_id}"
                    )
                if result.call_id not in pending:
                    raise ValueError(
                        f"Orphan tool output in model input: {result.call_id}"
                    )
                seen_outputs.add(result.call_id)
                pending.remove(result.call_id)
            if pending:
                raise ValueError(
                    "Tool outputs did not close all pending calls: "
                    f"{', '.join(sorted(pending))}"
                )
    if pending:
        raise ValueError(
            f"Unresolved tool use in model input: {', '.join(sorted(pending))}"
        )


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


__all__ = [
    "AssistantOutput",
    "InputContent",
    "InputDocument",
    "InputImage",
    "InputText",
    "ModelAssistantContent",
    "ModelInputItem",
    "ModelOutput",
    "ModelReasoningBlock",
    "ModelRequest",
    "ModelTextBlock",
    "ModelToolDefinition",
    "ModelToolUseBlock",
    "PromptStability",
    "ResolvedPromptSection",
    "SystemPrompt",
    "ToolOutput",
    "ToolOutputContent",
    "ToolOutputDocument",
    "ToolOutputImage",
    "ToolOutputs",
    "ToolOutputText",
    "UserInput",
    "validate_model_input",
]
