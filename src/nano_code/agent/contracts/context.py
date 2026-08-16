"""上下文规划和预算诊断值对象。"""

from dataclasses import dataclass, field

from .model import ModelRequest
from .session import ContentReplacement


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
    user_context_chars: int = 0
    attachment_chars: int = 0

    @property
    def estimated_input_chars(self) -> int:
        return (
            self.message_chars
            + self.system_chars
            + self.tool_schema_chars
            + self.user_context_chars
            + self.attachment_chars
        )

    @property
    def estimated_total_tokens(self) -> int:
        return self.estimated_input_tokens + self.reserved_output_tokens


@dataclass(frozen=True, slots=True)
class ContextPlan:
    """上下文策略结果：完整模型请求、预算诊断和待持久化决策。"""

    request: ModelRequest
    budget: ContextBudget | None = None
    new_content_replacements: tuple[ContentReplacement, ...] = field(
        default_factory=tuple
    )


__all__ = [
    "ContextBudget",
    "ContextPlan",
]
