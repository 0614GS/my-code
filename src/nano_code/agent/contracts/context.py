"""上下文规划和预算诊断值对象。"""

from dataclasses import dataclass, field
from typing import Literal

from nano_code.messages import SystemContextBlock, TextBlock
from nano_code.prompts import SystemPrompt

from .model import ModelInputMessage
from .session import ContentReplacement
from .tool import ToolDefinition

type EphemeralContextContentBlock = TextBlock | SystemContextBlock


@dataclass(frozen=True, slots=True)
class EphemeralContextMessage:
    """Non-history context before the model input normalization boundary."""

    role: Literal["user"]
    content: tuple[EphemeralContextContentBlock, ...]

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("A context message must contain at least one block")


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """一次请求各组成部分的可观察预算报告。"""

    message_limit_chars: int
    message_chars: int
    system_chars: int
    tool_schema_chars: int
    reserved_output_tokens: int
    last_actual_input_tokens: int | None
    incremental_tokens: int
    estimated_input_tokens: int
    workspace_context_chars: int = 0

    @property
    def estimated_input_chars(self) -> int:
        return (
            self.message_chars
            + self.system_chars
            + self.tool_schema_chars
            + self.workspace_context_chars
        )

    @property
    def estimated_total_tokens(self) -> int:
        return self.estimated_input_tokens + self.reserved_output_tokens


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """经过上下文策略处理、可交给任意模型 adapter 的请求计划。"""

    system_prompt: SystemPrompt
    messages: tuple[ModelInputMessage, ...]
    tools: tuple[ToolDefinition, ...]
    max_output_tokens: int
    budget: ContextBudget | None = None
    new_content_replacements: tuple[ContentReplacement, ...] = field(
        default_factory=tuple
    )
    workspace_context: tuple[ModelInputMessage, ...] = ()


__all__ = [
    "ContextBudget",
    "ContextPlan",
    "EphemeralContextContentBlock",
    "EphemeralContextMessage",
]
